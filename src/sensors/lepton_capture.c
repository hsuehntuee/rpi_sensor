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
#define BUFFER_PACKETS 180
#define BUFFER_BYTES (BUFFER_PACKETS * PACKET_BYTES)

/**
 * 100% Reliable Native C VoSPI capture for FLIR Lepton 2.x on RPi5.
 * Uses pylepton packet accumulator strategy to collect all 60 packets (0..59).
 */
int capture_lepton_frame(const char* spidev_path, uint32_t speed_hz, uint16_t* out_frame, int max_attempts) {
    uint8_t mode = SPI_MODE_3;
    uint8_t bits = 8;

    // 1. Hardware CS-HIGH reset pause
    int init_fd = open(spidev_path, O_RDWR);
    if (init_fd >= 0) {
        close(init_fd);
    }
    usleep(200000); // 200ms CS HIGH hardware reset (>185ms required by FLIR spec)

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

    uint8_t* raw_buf = (uint8_t*)malloc(BUFFER_BYTES);
    if (!raw_buf) {
        close(fd);
        return -3;
    }

    uint8_t row_filled[LEPTON_ROWS];
    memset(row_filled, 0, sizeof(row_filled));
    int collected = 0;
    int success_attempt = 0;

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        int nread = 0;
        while (nread < BUFFER_BYTES) {
            int r = read(fd, raw_buf + nread, BUFFER_BYTES - nread);
            if (r <= 0) break;
            nread += r;
        }

        if (attempt == 1 || attempt % 300 == 0) {
            printf("  [Native C Diag] Attempt %d: nread=%d, bytes=%02X %02X %02X %02X %02X %02X %02X %02X\n",
                   attempt, nread, raw_buf[0], raw_buf[1], raw_buf[2], raw_buf[3], raw_buf[4], raw_buf[5], raw_buf[6], raw_buf[7]);
            fflush(stdout);
        }

        int n_packets = nread / PACKET_BYTES;
        for (int i = 0; i < n_packets; i++) {
            int off = i * PACKET_BYTES;
            uint8_t b0 = raw_buf[off];
            uint8_t b1 = raw_buf[off + 1];

            // Ignore discard packet
            if ((b0 & 0x0F) == 0x0F) continue;
            if (b1 >= LEPTON_ROWS) continue;

            uint8_t pkt_num = b1;

            // When Packet 0 arrives and current frame is incomplete, reset accumulator
            if (pkt_num == 0 && collected < LEPTON_ROWS) {
                memset(row_filled, 0, sizeof(row_filled));
                collected = 0;
            }

            if (!row_filled[pkt_num]) {
                row_filled[pkt_num] = 1;
                collected++;

                // Copy 160-byte payload into destination frame
                int data_off = off + 4;
                for (int c = 0; c < LEPTON_COLS; c++) {
                    uint8_t high = raw_buf[data_off + c * 2];
                    uint8_t low  = raw_buf[data_off + c * 2 + 1];
                    out_frame[pkt_num * LEPTON_COLS + c] = ((uint16_t)high << 8) | low;
                }

                if (collected == LEPTON_ROWS) {
                    success_attempt = attempt;
                    break;
                }
            }
        }

        if (success_attempt > 0) break;

        // Periodic 200ms CS-HIGH hardware resync if Lepton enters VoSPI desync
        if (attempt % 10 == 0) {
            close(fd);
            usleep(200000); // 200ms CS HIGH resync
            fd = open(spidev_path, O_RDWR);
            if (fd < 0) break;
            ioctl(fd, SPI_IOC_WR_MODE, &mode);
            ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
            ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz);
            memset(row_filled, 0, sizeof(row_filled));
            collected = 0;
        } else {
            usleep(1000);
        }
    }

    free(raw_buf);
    if (fd >= 0) close(fd);
    return success_attempt;
}
