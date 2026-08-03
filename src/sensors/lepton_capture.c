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
#define BUFFER_PACKETS 180
#define BUFFER_BYTES (BUFFER_PACKETS * PACKET_BYTES)

/**
 * High-performance 1-byte sliding window VoSPI packet accumulator for FLIR Lepton.
 * Captures all 60 rows in < 20 milliseconds directly in C kernel IO calls.
 */
int capture_lepton_frame(const char* spidev_path, uint32_t speed_hz, uint16_t* out_frame, int max_attempts) {
    uint8_t mode = SPI_MODE_3;
    uint8_t bits = 8;

    int fd = open(spidev_path, O_RDWR);
    if (fd < 0) return -1;

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

    uint8_t collected[LEPTON_ROWS];
    memset(collected, 0, sizeof(collected));
    int total_collected = 0;
    int success_attempt = 0;

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        int nread = 0;
        while (nread < BUFFER_BYTES) {
            int r = read(fd, raw_buf + nread, BUFFER_BYTES - nread);
            if (r <= 0) break;
            nread += r;
        }

        if (nread < BUFFER_BYTES) {
            usleep(1000);
            continue;
        }

        // 1-byte sliding window packet scanner
        int pos = 0;
        while (pos <= BUFFER_BYTES - PACKET_BYTES) {
            uint8_t b0 = raw_buf[pos];
            uint8_t b1 = raw_buf[pos + 1];

            // Valid header test
            if ((b0 & 0x0F) != 0x0F && b1 < LEPTON_ROWS) {
                int data_off = pos + 4;
                uint32_t sum = 0;
                for (int c = 0; c < LEPTON_COLS; c++) {
                    uint8_t high = raw_buf[data_off + c * 2];
                    uint8_t low  = raw_buf[data_off + c * 2 + 1];
                    sum += ((uint16_t)high << 8) | low;
                }

                // Valid thermal payload filter (> 500 mean ADU)
                if (sum / LEPTON_COLS > 500) {
                    if (!collected[b1]) {
                        for (int c = 0; c < LEPTON_COLS; c++) {
                            uint8_t high = raw_buf[data_off + c * 2];
                            uint8_t low  = raw_buf[data_off + c * 2 + 1];
                            out_frame[b1 * LEPTON_COLS + c] = ((uint16_t)high << 8) | low;
                        }
                        collected[b1] = 1;
                        total_collected++;

                        if (total_collected == LEPTON_ROWS) {
                            success_attempt = attempt;
                            break;
                        }
                    }
                    pos += PACKET_BYTES;
                    continue;
                }
            }
            pos++; // Auto-realign 1 byte
        }

        if (success_attempt > 0) break;
        usleep(500);
    }

    free(raw_buf);
    if (fd >= 0) close(fd);
    return success_attempt;
}
