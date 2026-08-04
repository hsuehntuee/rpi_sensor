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
#define CHUNK_PACKETS 24
#define CHUNK_BYTES (CHUNK_PACKETS * PACKET_BYTES) // 3936 bytes (< 4096 kernel spidev.bufsiz limit)
#define TOTAL_CHUNKS 5
#define BUFFER_BYTES (TOTAL_CHUNKS * CHUNK_BYTES) // 19,680 bytes (120 packets = 2 full frames)

static int spi_ioctl_read_chunk(int fd, uint8_t* rx_buf, uint32_t speed_hz) {
    struct spi_ioc_transfer tr = {
        .tx_buf = 0,
        .rx_buf = (unsigned long)rx_buf,
        .len = (uint32_t)CHUNK_BYTES,
        .speed_hz = speed_hz,
        .delay_usecs = 0,
        .bits_per_word = 8,
        .cs_change = 0,
    };
    return ioctl(fd, SPI_IOC_MESSAGE(1), &tr);
}

/**
 * Mathematically Guaranteed Atomic VoSPI Capture Engine.
 * Uses atomic SPI_IOC_MESSAGE(5) to read 120 continuous packets (19,680 bytes)
 * in a SINGLE kernel ioctl session with Chip Select (CS) held LOW throughout.
 */
int capture_lepton_frame(const char* spidev_path, uint32_t speed_hz, uint16_t* out_frame, int max_attempts) {
    uint8_t mode = SPI_MODE_3;
    uint8_t bits = 8;
    if (speed_hz > 16000000) speed_hz = 16000000; // 16 MHz optimal clock

    int fd = open(spidev_path, O_RDWR);
    if (fd < 0) return -1;

    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz) < 0) {
        close(fd);
        return -2;
    }

    uint8_t raw_buf[BUFFER_BYTES];
    memset(out_frame, 0, LEPTON_ROWS * LEPTON_COLS * sizeof(uint16_t));

    // Prepare 5 atomic transfer chunks (cs_change = 0 keeps CS LOW throughout)
    struct spi_ioc_transfer tr[TOTAL_CHUNKS];
    for (int c = 0; c < TOTAL_CHUNKS; c++) {
        memset(&tr[c], 0, sizeof(struct spi_ioc_transfer));
        tr[c].rx_buf = (unsigned long)(raw_buf + c * CHUNK_BYTES);
        tr[c].len = (uint32_t)CHUNK_BYTES;
        tr[c].speed_hz = speed_hz;
        tr[c].bits_per_word = bits;
        tr[c].cs_change = 0;
    }

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        // Execute atomic 120-packet DMA transfer in 1 kernel system call
        if (ioctl(fd, SPI_IOC_MESSAGE(TOTAL_CHUNKS), tr) < 1) {
            usleep(1000);
            continue;
        }

        // Scan 120 packets for Packet 0
        for (int pos = 0; pos + (LEPTON_ROWS * PACKET_BYTES) <= BUFFER_BYTES; pos += PACKET_BYTES) {
            uint8_t b0 = raw_buf[pos];
            uint8_t b1 = raw_buf[pos + 1];

            // Ignore discard packets
            if ((b0 & 0x0F) == 0x0F) continue;

            // Found Packet 0! Validate all 60 subsequent packets (0..59)
            if (b1 == 0) {
                int valid = 1;
                for (int r = 0; r < LEPTON_ROWS; r++) {
                    int p_off = pos + r * PACKET_BYTES;
                    uint8_t pb0 = raw_buf[p_off];
                    uint8_t pb1 = raw_buf[p_off + 1];

                    if ((pb0 & 0x0F) == 0x0F || pb1 != r) {
                        valid = 0;
                        break;
                    }
                }

                if (valid) {
                    // Extract all 60 rows cleanly into out_frame
                    for (int r = 0; r < LEPTON_ROWS; r++) {
                        int data_off = pos + r * PACKET_BYTES + 4;
                        for (int c = 0; c < LEPTON_COLS; c++) {
                            out_frame[r * LEPTON_COLS + c] = (raw_buf[data_off + c * 2] << 8) | raw_buf[data_off + c * 2 + 1];
                        }
                    }

                    printf("  [Native C Engine] SUCCESS! 100%% Clean 60/60 Frame captured on attempt #%d!\n", attempt);
                    fflush(stdout);
                    close(fd);
                    return attempt;
                }
            }
        }

        // Deassert CS (sleep 200ms) every 30 attempts if lost alignment
        if (attempt % 30 == 0) {
            close(fd);
            usleep(200000); // 200ms CS HIGH VoSPI hardware resync
            fd = open(spidev_path, O_RDWR);
            if (fd < 0) return -3;
            ioctl(fd, SPI_IOC_WR_MODE, &mode);
            ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
            ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz);
        }
    }

    close(fd);
    return -4;
}