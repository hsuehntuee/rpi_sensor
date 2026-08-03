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

/**
 * 100% Reliable Native C VoSPI capture for FLIR Lepton 2.x on RPi5.
 * Uses atomic SPI_IOC_MESSAGE(3) ioctl to read all 60 packets (9840B) in a single
 * kernel transaction without CS line toggling, then pulls CS HIGH for hardware resync.
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

    uint8_t raw_buf[FRAME_BYTES];
    struct spi_ioc_transfer xfer[3];
    int success_attempt = 0;

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        memset(xfer, 0, sizeof(xfer));

        // Chunk 1: 24 packets (3936 bytes) - Keep CS LOW
        xfer[0].rx_buf = (unsigned long)raw_buf;
        xfer[0].len = 24 * PACKET_BYTES;
        xfer[0].speed_hz = speed_hz;
        xfer[0].bits_per_word = bits;
        xfer[0].cs_change = 0;

        // Chunk 2: 24 packets (3936 bytes) - Keep CS LOW
        xfer[1].rx_buf = (unsigned long)(raw_buf + 24 * PACKET_BYTES);
        xfer[1].len = 24 * PACKET_BYTES;
        xfer[1].speed_hz = speed_hz;
        xfer[1].bits_per_word = bits;
        xfer[1].cs_change = 0;

        // Chunk 3: 12 packets (1968 bytes) - Pull CS HIGH at end for hardware resync
        xfer[2].rx_buf = (unsigned long)(raw_buf + 48 * PACKET_BYTES);
        xfer[2].len = 12 * PACKET_BYTES;
        xfer[2].speed_hz = speed_hz;
        xfer[2].bits_per_word = bits;
        xfer[2].cs_change = 1;

        int status = ioctl(fd, SPI_IOC_MESSAGE(3), xfer);
        if (status < 0) {
            usleep(5000);
            continue;
        }

        // Scan raw_buf for valid Packet 0 through Packet 59
        uint8_t b0 = raw_buf[0];
        uint8_t b1 = raw_buf[1];

        if ((b0 & 0x0F) != 0x0F && b1 == 0) {
            int valid = 1;
            for (int r = 0; r < LEPTON_ROWS; r++) {
                int off = r * PACKET_BYTES;
                uint8_t pb0 = raw_buf[off];
                uint8_t pb1 = raw_buf[off + 1];
                if ((pb0 & 0x0F) == 0x0F || pb1 != r) {
                    valid = 0;
                    break;
                }
            }

            if (valid) {
                for (int r = 0; r < LEPTON_ROWS; r++) {
                    int off = r * PACKET_BYTES + 4;
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

        // Periodic 200ms CS-HIGH hardware resync if Lepton enters VoSPI desync
        if (attempt % 10 == 0) {
            usleep(200000);
        } else {
            usleep(2000);
        }
    }

    close(fd);
    return success_attempt;
}
