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

// 既然解除了 65536 限制，我們一次讀取 3 個完整的 Frame (29520 bytes)
// 這樣絕對能保證在連續的 CS LOW 期間內，抓到一個完整的 60 rows
#define BUFFER_BYTES (3 * LEPTON_ROWS * PACKET_BYTES)

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

    // 單一 ioctl 傳輸結構，不再切碎！
    struct spi_ioc_transfer tr = {
        .tx_buf = 0,
        .rx_buf = (unsigned long)raw_buf,
        .len = BUFFER_BYTES,
        .speed_hz = speed_hz,
        .delay_usecs = 0,
        .bits_per_word = 8,
        .cs_change = 0, // 告訴內核這筆大資料傳輸完畢前，不准動 CS
    };

    int success_attempt = 0;

    for (int attempt = 1; attempt <= max_attempts; attempt++) {
        // 【關鍵】一次性讀取 29520 bytes，CS 保持完美低電位
        if (ioctl(fd, SPI_IOC_MESSAGE(1), &tr) < 1) {
            usleep(1000);
            continue;
        }

        uint8_t collected[LEPTON_ROWS];
        memset(collected, 0, sizeof(collected));
        int total_collected = 0;

        int pos = 0;
        // 確保不會越界
        while (pos <= BUFFER_BYTES - PACKET_BYTES * 2) {
            uint8_t b0 = raw_buf[pos];
            uint8_t b1 = raw_buf[pos + 1];

            // 驗證 VoSPI Header
            if ((b0 & 0x0F) != 0x0F && b1 < LEPTON_ROWS) {
                // 【核心補強】驗證下一個封包標頭是否連續 (Packet N+1 或 Discard)，徹底排除 CRC 誤判導致的紅色垂直線
                uint8_t next_b0 = raw_buf[pos + PACKET_BYTES];
                uint8_t next_b1 = raw_buf[pos + PACKET_BYTES + 1];
                int is_seq = ((next_b0 & 0x0F) == 0x0F) || (next_b1 == (b1 + 1) % LEPTON_ROWS);

                if (is_seq) {
                    int data_off = pos + 4;
                    uint32_t sum = 0;
                    
                    // 先算平均值，過濾無效的垃圾封包
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
                                success_attempt = attempt;
                                break; // 成功收集 60 行，跳出解析迴圈
                            }
                        }
                        // 既然確認是正確的 Packet，直接往前跳一整個封包的大小，避免誤判
                        pos += PACKET_BYTES;
                        continue;
                    }
                }
            }
            // 找不到 Header，只往前推 1 byte 繼續對齊
            pos++;
        }

        if (success_attempt > 0) {
            break; // 成功，跳出最外層 attempt 迴圈
        }
        
        // 【終極保險】關閉 fd 讓 CS 腳位確實拉高 (HIGH)，休眠 200ms 重置 Lepton VoSPI 狀態機
        close(fd);
        usleep(200000); // 強制休眠 200ms (>185ms required)
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