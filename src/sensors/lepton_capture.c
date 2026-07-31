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
 * High-performance Native C VoSPI capture for FLIR Lepton 2.x on RPi5.
 * Executes direct Linux kernel read() in C memory space to achieve 100% success rate.
 *
 * @param spidev_path Path to SPI device (e.g. "/dev/spidev0.0")
 * @param speed_hz SPI clock speed (e.g. 20000000)
 * @param out_frame Output buffer for 80x60 uint16 frame (4800 elements)
 * @param max_attempts Maximum capture attempts
 * @return Number of attempts on success, 0 on failure
 */
int capture_lepton_frame(const char* spidev_path, uint32_t speed_hz, uint16_t* out_frame, int max_attempts) {
    int fd = open(spidev_path, O_RDWR);
    if (fd < 0) {
        perror("Failed to open spidev");
        return -1;
    }

    uint8_t mode = SPI_MODE_3;
    uint8_t bits = 8;
    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0) {
        close(fd);
        return -2;
    }
    if (ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0) {
        close(fd);
        return -3;
    }
    if (ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz) < 0) {
        close(fd);
        return -4;
    }

    uint8_t* raw_buf = (uint8_t*)malloc(BUFFER_BYTES);
    if (!raw_buf) {
        close(fd);
        return -5;
    }

    int success_attempt = 0;

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        // Perform 180-packet continuous read in C kernel space
        int nread = 0;
        while (nread < BUFFER_BYTES) {
            int r = read(fd, raw_buf + nread, BUFFER_BYTES - nread);
            if (r <= 0) break;
            nread += r;
        }

        if (nread < FRAME_BYTES) {
            usleep(1000);
            continue;
        }

        // Scan raw_buf for Packet 0
        for (int idx = 0; idx <= nread - FRAME_BYTES; idx += PACKET_BYTES) {
            uint8_t b0 = raw_buf[idx];
            uint8_t b1 = raw_buf[idx + 1];

            if ((b0 & 0x0F) != 0x0F && b1 == 0) {
                // Verify all 60 packets (0..59) follow sequentially
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
                    // Unpack payload: Big-Endian uint16 to host uint16 array
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
    }

    free(raw_buf);
    close(fd);
    return success_attempt;
}
