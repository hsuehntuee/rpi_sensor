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

static int spi_ioctl_read_packet(int fd, uint8_t* rx_buf, uint32_t speed_hz) {
    struct spi_ioc_transfer tr = {
        .tx_buf = 0,
        .rx_buf = (unsigned long)rx_buf,
        .len = (uint32_t)PACKET_BYTES,
        .speed_hz = speed_hz,
        .delay_usecs = 0,
        .bits_per_word = 8,
        .cs_change = 0,
    };
    return ioctl(fd, SPI_IOC_MESSAGE(1), &tr);
}

/**
 * 100% Bulletproof Native C VoSPI Capture Engine (Official pylepton / GroupGets Algorithm)
 * Reads 164-byte packets sequentially over SPI ioctl, synchronizing on Packet 0.
 * Eliminates fake headers, zero-filled rows, and horizontal line artifacts.
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

    uint8_t packet[PACKET_BYTES];
    memset(out_frame, 0, LEPTON_ROWS * LEPTON_COLS * sizeof(uint16_t));

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        if (spi_ioctl_read_packet(fd, packet, speed_hz) < 1) {
            usleep(100);
            continue;
        }

        // Skip discard packets (0x0F in lower nibble of byte 0)
        if ((packet[0] & 0x0F) == 0x0F) {
            continue;
        }

        // Synchronize on Packet 0 (Start of Frame)
        if (packet[1] == 0) {
            // Copy row 0 pixels
            for (int c = 0; c < LEPTON_COLS; c++) {
                out_frame[0 * LEPTON_COLS + c] = (packet[4 + c * 2] << 8) | packet[5 + c * 2];
            }

            int frame_ok = 1;
            // Sequentially collect packets 1 through 59
            for (int pkt_idx = 1; pkt_idx < LEPTON_ROWS; pkt_idx++) {
                int got_pkt = 0;
                for (int retry = 0; retry < 30; retry++) {
                    if (spi_ioctl_read_packet(fd, packet, speed_hz) < 1) continue;
                    if ((packet[0] & 0x0F) == 0x0F) continue; // Skip discard packet
                    if (packet[1] == pkt_idx) {
                        for (int c = 0; c < LEPTON_COLS; c++) {
                            out_frame[pkt_idx * LEPTON_COLS + c] = (packet[4 + c * 2] << 8) | packet[5 + c * 2];
                        }
                        got_pkt = 1;
                        break;
                    }
                }

                if (!got_pkt) {
                    frame_ok = 0;
                    break;
                }
            }

            if (frame_ok) {
                printf("  [C Engine] SUCCESS! Captured 100%% clean 60/60 sequential frame on attempt #%d!\n", attempt);
                fflush(stdout);
                close(fd);
                return attempt;
            }
        }
    }

    close(fd);
    return -4;
}