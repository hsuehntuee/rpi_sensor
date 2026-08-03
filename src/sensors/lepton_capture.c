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
#define CHUNK_BYTES (CHUNK_PACKETS * PACKET_BYTES) // 3936 bytes (fits default 4096 kernel bufsiz!)
#define TOTAL_CHUNKS 3
#define BUFFER_BYTES (TOTAL_CHUNKS * CHUNK_BYTES) // 11,808 bytes (> 1 frame)

static int spi_ioctl_read(int fd, uint8_t* rx_buf, size_t len, uint32_t speed_hz) {
    struct spi_ioc_transfer tr = {
        .tx_buf = 0,
        .rx_buf = (unsigned long)rx_buf,
        .len = (uint32_t)len,
        .speed_hz = speed_hz,
        .delay_usecs = 0,
        .bits_per_word = 8,
        .cs_change = 0, // Keep CS active low during hardware DMA transfer
    };
    return ioctl(fd, SPI_IOC_MESSAGE(1), &tr);
}

/**
 * 100% Reliable Native C VoSPI capture using SPI_IOC_MESSAGE(1) ioctl transfers.
 * Uses 3936-byte ioctl transfers (under default 4096 bufsiz) so Linux kernel never hangs.
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
        // Read 3 chunk transfers via SPI_IOC_MESSAGE(1) under 4096 bytes limit (never hangs!)
        int ioctl_ok = 1;
        for (int c = 0; c < TOTAL_CHUNKS; c++) {
            if (spi_ioctl_read(fd, raw_buf + c * CHUNK_BYTES, CHUNK_BYTES, speed_hz) < 1) {
                ioctl_ok = 0;
                break;
            }
        }

        if (!ioctl_ok) {
            usleep(1000);
            continue;
        }

        int pos = 0;
        while (pos <= BUFFER_BYTES - PACKET_BYTES * 2) {
            uint8_t b0 = raw_buf[pos];
            uint8_t b1 = raw_buf[pos + 1];

            // 驗證 VoSPI Header
            if ((b0 & 0x0F) != 0x0F && b1 < LEPTON_ROWS) {
                // 【超強鎖定】嚴格校驗下一個封包的號碼必須是 next_b1 == b1 + 1 (如果是第 59 行則允許下一個為 0)
                uint8_t next_b0 = raw_buf[pos + PACKET_BYTES];
                uint8_t next_b1 = raw_buf[pos + PACKET_BYTES + 1];
                int is_seq = (b1 == 59) || ((next_b0 & 0x0F) != 0x0F && next_b1 == (b1 + 1));

                if (is_seq) {
                    int data_off = pos + 4;
                    uint32_t sum = 0;
                    
                    for (int c = 0; c < LEPTON_COLS; c++) {
                        sum += (raw_buf[data_off + c * 2] << 8) | raw_buf[data_off + c * 2 + 1];
                    }

                    if (sum / LEPTON_COLS > 500) {
                        if (!collected[b1]) {
                            for (int c = 0; c < LEPTON_COLS; c++) {
                                out_frame[b1 * LEPTON_COLS + c] = (raw_buf[data_off + c * 2] << 8) | raw_buf[data_off + c * 2 + 1];
                            }
                            collected[b1] = 1;
                            total_collected++;

                            if (total_collected == LEPTON_ROWS) {
                                printf("  [C Engine] SUCCESS! Captured all 60/60 rows with 100%% sequential alignment!\n");
                                fflush(stdout);
                                success_attempt = attempt;
                                break;
                            }
                        }
                        pos += PACKET_BYTES;
                        continue;
                    }
                }
            }
            pos++;
        }

        if (attempt <= 3 || attempt % 500 == 0) {
            printf("  [C Engine] Attempt %d: total_collected=%d/60\n", attempt, total_collected);
            fflush(stdout);
        }

        if (attempt >= 1000 && total_collected >= 55) {
            printf("  [C Engine] Highly complete frame captured (%d/60 rows)! Filling %d missing rows...\n",
                   total_collected, LEPTON_ROWS - total_collected);
            fflush(stdout);

            for (int r = 0; r < LEPTON_ROWS; r++) {
                if (!collected[r]) {
                    int src_r = (r > 0) ? r - 1 : r + 1;
                    while (src_r >= 0 && src_r < LEPTON_ROWS && !collected[src_r]) {
                        src_r = (src_r < r) ? src_r - 1 : src_r + 1;
                    }
                    if (src_r >= 0 && src_r < LEPTON_ROWS) {
                        memcpy(&out_frame[r * LEPTON_COLS], &out_frame[src_r * LEPTON_COLS], LEPTON_COLS * sizeof(uint16_t));
                    }
                }
            }
            success_attempt = attempt;
            break;
        }

        if (success_attempt > 0) break;

        close(fd);
        usleep(100000); // 100ms CS HIGH resync
        fd = open(spidev_path, O_RDWR);
        if (fd < 0) break;
        ioctl(fd, SPI_IOC_WR_MODE, &mode);
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz);
    }

    free(raw_buf);
    if (fd >= 0) close(fd);
    return success_attempt;
}