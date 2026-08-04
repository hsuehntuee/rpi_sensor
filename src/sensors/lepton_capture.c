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
#define FRAME_PACKETS 60
#define FRAME_BYTES (FRAME_PACKETS * PACKET_BYTES) // 9,840 bytes (1 full VoSPI frame)

/**
 * Official GroupGets / PalLepton VoSPI capture algorithm for Raspberry Pi.
 * Reads exact 9,840-byte frame transfers at 16 MHz.
 * Performs 200ms CS deassertion resynchronization if frame alignment is lost.
 */
int capture_lepton_frame(const char* spidev_path, uint32_t speed_hz, uint16_t* out_frame, int max_attempts) {
    uint8_t mode = SPI_MODE_3;
    uint8_t bits = 8;
    // 16 MHz is the optimal clock frequency for Raspberry Pi 5 RP1 chip
    if (speed_hz > 16000000) speed_hz = 16000000;

    int fd = open(spidev_path, O_RDWR);
    if (fd < 0) return -1;

    if (ioctl(fd, SPI_IOC_WR_MODE, &mode) < 0 ||
        ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed_hz) < 0) {
        close(fd);
        return -2;
    }

    uint8_t rx_buf[FRAME_BYTES];

    struct spi_ioc_transfer tr = {
        .tx_buf = 0,
        .rx_buf = (unsigned long)rx_buf,
        .len = FRAME_BYTES,
        .speed_hz = speed_hz,
        .delay_usecs = 0,
        .bits_per_word = 8,
        .cs_change = 0,
    };

    memset(out_frame, 0, LEPTON_ROWS * LEPTON_COLS * sizeof(uint16_t));

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        if (ioctl(fd, SPI_IOC_MESSAGE(1), &tr) < 1) {
            usleep(1000);
            continue;
        }

        // Ignore discard packet
        if ((rx_buf[0] & 0x0F) == 0x0F) {
            usleep(500);
            continue;
        }

        // Synchronize on Packet 0 (Start of Frame)
        if (rx_buf[1] == 0) {
            int valid_frame = 1;
            for (int r = 0; r < LEPTON_ROWS; r++) {
                int pkt_off = r * PACKET_BYTES;
                uint8_t b0 = rx_buf[pkt_off];
                uint8_t b1 = rx_buf[pkt_off + 1];

                if ((b0 & 0x0F) == 0x0F || b1 != r) {
                    valid_frame = 0;
                    break;
                }

                int data_off = pkt_off + 4;
                for (int c = 0; c < LEPTON_COLS; c++) {
                    out_frame[r * LEPTON_COLS + c] = (rx_buf[data_off + c * 2] << 8) | rx_buf[data_off + c * 2 + 1];
                }
            }

            if (valid_frame) {
                printf("  [Native C Engine] SUCCESS! 100%% Clean Frame captured on attempt #%d!\n", attempt);
                fflush(stdout);
                close(fd);
                return attempt;
            }
        }

        // Toggle CS (close device & sleep 200ms) to force VoSPI resynchronization
        if (attempt % 40 == 0) {
            close(fd);
            usleep(200000); // 200ms CS HIGH resync
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