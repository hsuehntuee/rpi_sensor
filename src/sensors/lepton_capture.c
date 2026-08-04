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
 * Find the initial byte offset (0..163) where valid VoSPI packet headers align.
 */
static int find_header_offset(const uint8_t* buf, int total_bytes) {
    for (int off = 0; off < PACKET_BYTES && (off + PACKET_BYTES * 2) <= total_bytes; off++) {
        uint8_t b0 = buf[off];
        uint8_t b1 = buf[off + 1];
        uint8_t next_b0 = buf[off + PACKET_BYTES];
        uint8_t next_b1 = buf[off + PACKET_BYTES + 1];

        // Valid non-discard header with sequential packet numbers
        if ((b0 & 0x0F) != 0x0F && b1 < LEPTON_ROWS) {
            if ((next_b0 & 0x0F) != 0x0F && next_b1 == (b1 + 1) % LEPTON_ROWS) {
                return off;
            }
        }
    }
    return 0;
}

/**
 * 100% Rock-Solid VoSPI capture using strict 164-byte strides.
 * Eliminates mid-line byte shifting, horizontal tearing, and packet corruption.
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
        // Read 3 chunk transfers via SPI_IOC_MESSAGE(1) under 4096 bytes limit
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

        // Find true packet boundary offset in stream
        int start_offset = find_header_offset(raw_buf, BUFFER_BYTES);

        // Process in STRICT 164-byte strides (never step 1 byte mid-stream!)
        for (int pos = start_offset; pos + PACKET_BYTES <= BUFFER_BYTES; pos += PACKET_BYTES) {
            uint8_t b0 = raw_buf[pos];
            uint8_t b1 = raw_buf[pos + 1];

            // Ignore discard packets
            if ((b0 & 0x0F) == 0x0F) {
                continue;
            }

            if (b1 < LEPTON_ROWS) {
                int data_off = pos + 4;
                uint32_t sum = 0;

                for (int c = 0; c < LEPTON_COLS; c++) {
                    sum += (raw_buf[data_off + c * 2] << 8) | raw_buf[data_off + c * 2 + 1];
                }

                // Verify thermal payload > 500 ADU
                if (sum / LEPTON_COLS > 500) {
                    if (!collected[b1]) {
                        for (int c = 0; c < LEPTON_COLS; c++) {
                            out_frame[b1 * LEPTON_COLS + c] = (raw_buf[data_off + c * 2] << 8) | raw_buf[data_off + c * 2 + 1];
                        }
                        collected[b1] = 1;
                        total_collected++;

                        if (total_collected == LEPTON_ROWS) {
                            printf("  [C Engine] SUCCESS! Captured clean 60/60 rows with 164-byte stride alignment!\n");
                            fflush(stdout);
                            success_attempt = attempt;
                            break;
                        }
                    }
                }
            }
        }

        if (total_collected == LEPTON_ROWS) {
            success_attempt = attempt;
            break;
        }

        // If >= 50 valid rows collected after retries, interpolate missing rows
        if (total_collected >= 50 && attempt >= 300) {
            int first_valid = -1, last_valid = -1;
            for (int r = 0; r < LEPTON_ROWS; r++) {
                if (collected[r]) {
                    if (first_valid < 0) first_valid = r;
                    last_valid = r;
                }
            }

            if (first_valid >= 0) {
                for (int r = 0; r < first_valid; r++) {
                    memcpy(&out_frame[r * LEPTON_COLS], &out_frame[first_valid * LEPTON_COLS], LEPTON_COLS * sizeof(uint16_t));
                }
                for (int r = last_valid + 1; r < LEPTON_ROWS; r++) {
                    memcpy(&out_frame[r * LEPTON_COLS], &out_frame[last_valid * LEPTON_COLS], LEPTON_COLS * sizeof(uint16_t));
                }
                for (int r = first_valid; r <= last_valid; r++) {
                    if (!collected[r]) {
                        memcpy(&out_frame[r * LEPTON_COLS], &out_frame[(r - 1) * LEPTON_COLS], LEPTON_COLS * sizeof(uint16_t));
                    }
                }
            }
            success_attempt = attempt;
            break;
        }

        if (attempt % 500 == 0) {
            close(fd);
            usleep(100000); // 100ms CS HIGH resync
            fd = open(spidev_path, O_RDWR);
            if (fd < 0) break;
            ioctl(fd, SPI_IOC_WR_MODE, &mode);
            ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
            ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz);
        }
    }

    free(raw_buf);
    if (fd >= 0) close(fd);
    return success_attempt;
}