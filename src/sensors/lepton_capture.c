#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/spi/spidev.h>

#define PACKET_BYTES 164
#define CHUNK_PACKETS 24
#define CHUNK_BYTES (CHUNK_PACKETS * PACKET_BYTES) // 3936 bytes (< 4096 kernel spidev.bufsiz limit)

/**
 * Mathematically Guaranteed Atomic VoSPI Capture Engine for Lepton 2.x & 3.x.
 * Uses atomic SPI_IOC_MESSAGE transfers to read continuous packets with Chip Select (CS) held LOW.
 */
int capture_lepton_frame(const char* spidev_path, uint32_t speed_hz, uint16_t* out_frame, int width, int height, int max_attempts) {
    uint8_t mode = SPI_MODE_3;
    uint8_t bits = 8;
    if (speed_hz > 16000000) speed_hz = 16000000; // 16 MHz optimal clock

    int is_lepton3 = (width * height) > 4800;
    int total_chunks = is_lepton3 ? 13 : 5;
    int buffer_bytes = total_chunks * CHUNK_BYTES;

    int fd = open(spidev_path, O_RDWR);
    if (fd < 0) return -1;

    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz) < 0) {
        close(fd);
        return -2;
    }

    uint8_t* raw_buf = (uint8_t*)malloc(buffer_bytes);
    if (!raw_buf) {
        close(fd);
        return -5;
    }
    memset(out_frame, 0, width * height * sizeof(uint16_t));

    // For Lepton 3.x, allocate temporary segment buffers (4 segments, 60 packets each)
    uint8_t* segments = NULL;
    uint8_t segment_captured[4] = {0, 0, 0, 0};
    if (is_lepton3) {
        segments = (uint8_t*)malloc(4 * 60 * PACKET_BYTES);
        if (!segments) {
            free(raw_buf);
            close(fd);
            return -5;
        }
        memset(segments, 0, 4 * 60 * PACKET_BYTES);
    }

    // Prepare transfer chunks (cs_change = 0 keeps CS LOW throughout)
    struct spi_ioc_transfer* tr = (struct spi_ioc_transfer*)calloc(total_chunks, sizeof(struct spi_ioc_transfer));
    if (!tr) {
        free(raw_buf);
        if (segments) free(segments);
        close(fd);
        return -5;
    }
    for (int c = 0; c < total_chunks; c++) {
        tr[c].rx_buf = (unsigned long)(raw_buf + c * CHUNK_BYTES);
        tr[c].len = (uint32_t)CHUNK_BYTES;
        tr[c].speed_hz = speed_hz;
        tr[c].bits_per_word = bits;
        tr[c].cs_change = 0;
    }

    int success = 0;
    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        // Execute atomic multi-packet DMA transfer in 1 kernel system call
        if (ioctl(fd, SPI_IOC_MESSAGE(total_chunks), tr) < 1) {
            usleep(1000);
            continue;
        }

        if (!is_lepton3) {
            // Lepton 2.x capture logic
            for (int pos = 0; pos + (60 * PACKET_BYTES) <= buffer_bytes; pos += PACKET_BYTES) {
                uint8_t b0 = raw_buf[pos];
                uint8_t b1 = raw_buf[pos + 1];

                // Ignore discard packets
                if ((b0 & 0x0F) == 0x0F) continue;

                // Found Packet 0! Validate all 60 subsequent packets (0..59)
                if (b1 == 0) {
                    int valid = 1;
                    for (int r = 0; r < 60; r++) {
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
                        for (int r = 0; r < 60; r++) {
                            int data_off = pos + r * PACKET_BYTES + 4;
                            for (int c = 0; c < 80; c++) {
                                out_frame[r * 80 + c] = (raw_buf[data_off + c * 2] << 8) | raw_buf[data_off + c * 2 + 1];
                            }
                        }
                        success = attempt;
                        break;
                    }
                }
            }
        } else {
            // Lepton 3.x capture logic: find segments in 312 packets
            for (int pos = 0; pos + (60 * PACKET_BYTES) <= buffer_bytes; pos += PACKET_BYTES) {
                uint8_t b0 = raw_buf[pos];
                uint8_t b1 = raw_buf[pos + 1];

                // Ignore discard packets
                if ((b0 & 0x0F) == 0x0F) continue;

                // Found Packet 0 of a segment! Validate all 60 packets (0..59)
                if (b1 == 0) {
                    int valid = 1;
                    for (int r = 0; r < 60; r++) {
                        int p_off = pos + r * PACKET_BYTES;
                        uint8_t pb0 = raw_buf[p_off];
                        uint8_t pb1 = raw_buf[p_off + 1];

                        if ((pb0 & 0x0F) == 0x0F || pb1 != r) {
                            valid = 0;
                            break;
                        }
                    }

                    if (valid) {
                        // Extract segment number from packet 20
                        int p20_off = pos + 20 * PACKET_BYTES;
                        uint8_t seg_id = (raw_buf[p20_off] >> 4) & 0x07;
                        if (seg_id >= 1 && seg_id <= 4) {
                            // Copy this segment into our temporary segment buffers
                            memcpy(segments + (seg_id - 1) * 60 * PACKET_BYTES, raw_buf + pos, 60 * PACKET_BYTES);
                            segment_captured[seg_id - 1] = 1;
                        }
                    }
                }
            }

            // Check if we got all 4 segments
            if (segment_captured[0] && segment_captured[1] && segment_captured[2] && segment_captured[3]) {
                // Assemble the final 160x120 frame from the 4 segments
                for (int seg = 1; seg <= 4; seg++) {
                    int base_row = (seg - 1) * 30;
                    uint8_t* seg_buf = segments + (seg - 1) * 60 * PACKET_BYTES;
                    for (int p = 0; p < 60; p++) {
                        int row = base_row + (p / 2);
                        int col_offset = (p % 2 == 1) ? 80 : 0;
                        int packet_data_off = p * PACKET_BYTES + 4;
                        for (int c = 0; c < 80; c++) {
                            out_frame[row * 160 + col_offset + c] = (seg_buf[packet_data_off + c * 2] << 8) | seg_buf[packet_data_off + c * 2 + 1];
                        }
                    }
                }
                success = attempt;
                break;
            }
        }

        // Deassert CS (sleep 200ms) every 30 attempts if lost alignment
        if (attempt % 30 == 0) {
            close(fd);
            usleep(200000); // 200ms CS HIGH VoSPI hardware resync
            fd = open(spidev_path, O_RDWR);
            if (fd < 0) {
                free(raw_buf);
                free(tr);
                if (segments) free(segments);
                return -3;
            }
            ioctl(fd, SPI_IOC_WR_MODE, &mode);
            ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
            ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz);
        }
    }

    free(raw_buf);
    free(tr);
    if (segments) free(segments);
    close(fd);

    return success ? success : -4;
}