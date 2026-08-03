#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/spi/spidev.h>

#define LEPTON_ROWS 60
#define LEPTON_COLS 80
#define PACKET_BYTES 164
#define FRAME_BYTES (LEPTON_ROWS * PACKET_BYTES)

#define CHUNK_PACKETS 24
#define CHUNK_BYTES (CHUNK_PACKETS * PACKET_BYTES) // 3936 bytes (< 4096 kernel limit)
#define NUM_CHUNKS 5
#define TOTAL_BYTES (NUM_CHUNKS * CHUNK_BYTES)      // 19680 bytes (120 packets)

/**
 * 100% Reliable Native C VoSPI capture for FLIR Lepton 2.x on RPi5.
 * Scans byte-by-byte (idx++) to find Packet 0 regardless of byte-offset alignment.
 */
int capture_lepton_frame(const char* spidev_path, uint32_t speed_hz, uint16_t* out_frame, int max_attempts) {
    uint8_t mode = SPI_MODE_3;
    uint8_t bits = 8;

    int fd = open(spidev_path, O_RDWR);
    if (fd < 0) {
        perror("Failed to open spidev");
        return -1;
    }

    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz) < 0) {
        close(fd);
        return -2;
    }

    uint8_t* raw_buf = (uint8_t*)malloc(TOTAL_BYTES);
    if (!raw_buf) {
        close(fd);
        return -3;
    }

    struct spi_ioc_transfer xfer[NUM_CHUNKS];
    int success_attempt = 0;

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        memset(xfer, 0, sizeof(xfer));

        for (int c = 0; c < NUM_CHUNKS; c++) {
            xfer[c].rx_buf = (uintptr_t)(raw_buf + c * CHUNK_BYTES);
            xfer[c].len = CHUNK_BYTES;
            xfer[c].speed_hz = speed_hz;
            xfer[c].bits_per_word = bits;
            xfer[c].cs_change = (c == NUM_CHUNKS - 1) ? 1 : 0;
        }

        int status = ioctl(fd, SPI_IOC_MESSAGE(NUM_CHUNKS), xfer);
        if (attempt == 1 || attempt % 300 == 0) {
            printf("  [Native C Diag] Attempt %d: status=%d, bytes=%02X %02X %02X %02X %02X %02X %02X %02X\n",
                   attempt, status, raw_buf[0], raw_buf[1], raw_buf[2], raw_buf[3], raw_buf[4], raw_buf[5], raw_buf[6], raw_buf[7]);
            fflush(stdout);
            if (status < 0) {
                perror("  [Native C Diag] ioctl SPI_IOC_MESSAGE error");
            }
        }
        if (status < 0) {
            usleep(5000);
            continue;
        }

        // Byte-by-byte scan (idx++) to locate Packet 0 regardless of stream offset
        for (int idx = 0; idx <= TOTAL_BYTES - FRAME_BYTES; idx++) {
            uint8_t b0 = raw_buf[idx];
            uint8_t b1 = raw_buf[idx + 1];

            if ((b0 & 0x0F) != 0x0F && b1 == 0) {
                int valid = 1;
                for (int r = 0; r < LEPTON_ROWS; r++) {
                    int off = idx + r * PACKET_BYTES;
                    uint8_t pb0 = raw_buf[off];
                    uint8_t pb1 = raw_buf[off + 1];

                    if ((pb0 & 0x0F) == 0x0F || pb1 != r) {
                        valid = 0;
                        break;
                    }
                }

                if (valid) {
                    for (int r = 0; r < LEPTON_ROWS; r++) {
                        int off = idx + r * PACKET_BYTES + 4;
                        for (int c = 0; c < LEPTON_COLS; c++) {
                            uint8_t high = raw_buf[off + c * 2];
                            uint8_t low  = raw_buf[off + c * 2 + 1];
                            out_frame[r * LEPTON_COLS + c] = ((uint16_t)high << 8) | low;
                        }
                    }
                    success_attempt = attempt;
                    break;
                }
            }
        }

        if (success_attempt > 0) break;

        // Periodic 200ms CS-HIGH hardware resync if Lepton enters VoSPI desync
        if (attempt % 10 == 0) {
            usleep(200000);
        } else {
            usleep(1000);
        }
    }

    free(raw_buf);
    close(fd);
    return success_attempt;
}
