# NanoKVM の OS イメージ（akkijp フォーク）

NanoKVM の microSD に書くイメージ（起動区画＋ルートファイルシステム）を、Sipeed の SDK から作り直す。
上流の公式イメージ 1.4.3 は Buildroot 2023.11.2 で作られている。ここでは **Buildroot 2025.02.x LTS（2028 年 3 月までサポート）** に上げ、フォークのアプリ 2.5.4.1 を入れたイメージを作る。

- 2024.02 LTS は使わない。Buildroot は 2025.02 から奇数年の最初の版を 3 年の LTS にした。2024.02 は既にサポートが終わっている
- カーネル（5.10.4）・U-Boot・FSBL は SDK のまま。Buildroot が作るのはルートファイルシステムだけ（カーネルの CVE は下の見張りで追う）

## 材料

| 材料 | 版・固定のしかた |
|---|---|
| SDK | [sipeed/LicheeRV-Nano-Build](https://github.com/sipeed/LicheeRV-Nano-Build) a4d8622（2026-11-09） |
| ツールチェーン | [sophgo/host-tools](https://github.com/sophgo/host-tools) 103c66f（GCC 10.2・musl。SDK の README のとおり `host-tools/` に置く） |
| Buildroot | 2025.02.20（`buildroot-2025.02.20.tar.gz.sign` の SHA-256 `44904ee4…` を apply.sh が確かめる） |
| ビルド環境 | `os/Dockerfile`（ubuntu:22.04 をダイジェストで、apt を日付つきスナップショットで固定） |
| アプリ | フォークのリリース 2.5.4.1（`latest.json` の SHA-512 を確かめて `/kvmapp` に入れる） |
| 比べる相手 | 公式イメージ `20260610_NanoKVM_Rev1_4_3.img`（GitHub の SHA-256 `b03f3be9…`） |

## 作り方（nkbuild で）

```bash
mkdir -p ~/os && cd ~/os
git clone --depth=1 https://github.com/sipeed/LicheeRV-Nano-Build sdk
git clone --depth=1 https://github.com/sophgo/host-tools sdk/host-tools
# フォークの os/ を ~/os に置く（または akkijp/NanoKVM の akki/os を clone して os/ を使う）
NANOKVM_RELEASE=~/releases/2.5.4.1 ./build.sh ~/os/sdk br2025
# できたイメージ: ~/os/sdk-br2025-nanokvm/install/soc_sg2002_licheervnano_sd/images/*.img
```

`build.sh` は SDK を作業用の場所に写してから変える（元の clone は汚さない）。1 回のビルドは nkbuild（8 コア・8 GB）で 1〜2 時間、ディスクは 1 つの作業場所で 40 GB ほど使う。

- `baseline`: SDK のまま（Buildroot 2023.11.2）。比べるため
- `br2025`: Buildroot を 2025.02.20 に替える（`os/buildroot-2025.02/apply.sh`）
- `NANOKVM_RELEASE=…`: NanoKVM の層を重ねる（`os/nanokvm/apply.sh`）

## 2025.02 に移すときに変えたこと（`os/buildroot-2025.02/`）

Sipeed が Buildroot 2023.11.2 に足したものを移した: `board/cvitek`、defconfig 2 つ、9 パッケージ（aic8800-sdio-firmware・axp2101・cvitek-riscv64-musl-sysroot・gb-test-roms・lcdtest・mgba・sg2002-codec-firmware・tpudemo-sg200x・xuantie-gdb）、freetype の修正、busybox の exFAT。python-pyqt5-sip・opencv4 4.9 以上・numpy の riscv は 2025.02 に既にある。

SDK のツールチェーン（GCC 10・古い musl）のための直し: zstd 1.5.7 は glibc のときだけ `qsort_r` を使う（`patches/zstd/`）。eudev と gdb だけ `-ztext` を外す（2025.02 は musl のとき全部のプログラムを `-ztext` でリンクするが、この GCC では udevadm に TEXTREL が残る。公式 1.4.3 の udevadm も TEXTREL で、機器で動いている）。

defconfig は Sipeed のもの（全項目を書き出した形）を最小の形（savedefconfig）にしてから使う。全項目の形だと、既定値のままの古い項目で 2025.02 の legacy の検査が止まる。消えたパッケージ（ramspeed・obsidian-cursors）と gdb 13 の指定は `defconfig.fixup` で消す。

Buildroot 自身の都合で外れるもの（NanoKVM のアプリは使わない）: protobuf・mosh・opencv4 の dnn/objdetect/stitching（2025.02 は musl に ucontext が無いことを正しく扱い、abseil が要る protobuf が組めない）、python-selenium（Rust に riscv64 musl の対象が無い）、ffmpeg の avresample（ffmpeg 6 で廃止）、tiff の jbig。

## NanoKVM の層（`os/nanokvm/`）

公式イメージ 1.4.3 を開いて確かめた中身に合わせる。

- メモリ: ION 75 MiB（公式の dtb の ion-region が 0x4b00000）、起動ロゴ 5632 KiB
- 起動区画: `usb.dev usb.disk0 usb.rndis0 wifi.sta gt9xx logo.jpeg ver`（汎用イメージの `usb.ncm` の代わり）
- パッケージ: 公式に 1 つもファイルが無いもの（Qt・フォント・カーソル・tslib・PyQt5・babel・lftp・vsftpd・android-tools・fbv・Kerberos）を外し、ser2net・OpenIPMI を足す。tailscale は入れない（アプリが必要なときに最新版を入れる）
- 公式から外したもの（`packages.remove` に理由）: bind（公式はクライアントの道具だけ。bind 9.20 が要る liburcu が SDK の GCC 10 でリンクできない）、gdb・gdbserver（KVM に要らない。gdb 15 は SDK の musl のヘッダで組めない）。net-snmp も入らなくなった（2025.02 の OpenIPMI が引き込まない。アプリは使わない）
- `/etc/init.d`: 公式と同じ 27 本だけ（`init.d.keep`。ほかは post-build で消す）。dbus は 2025.02 で `S30dbus-daemon` に改名された。NanoKVM のものはアプリの `system/init.d` から
- `/kvmapp`: フォークのリリース。初回起動の印 `kvm_new_app`・`kvm_new_img` も公式と同じに置く

## 脆弱性（CVE）の見張り（`os/cve/`）

- `scan.sh`（nkbuild）: Buildroot の設定から SBOM（CycloneDX、`utils/generate-cyclonedx`）を作り、SDK が別に作るカーネル（5.10.4）を足して、NVD の写し（[fkie-cad/nvd-json-data-feeds](https://github.com/fkie-cad/nvd-json-data-feeds)、浅い clone）と照合する（`support/scripts/cve-check`）。ビルドしなくても回せる。「device」（いま機器に入っている 2023.11.2 の構成）と「rebuild」（2025.02 ＋ NanoKVM の層）の 2 つ
- `watch.sh`（Mac）: nkbuild で scan.sh を回し、前回からの新しい CVE だけを Codex（読み取りのみ）に渡し、`triage-prompt.md` の基準で「すぐ対応／次の版で／関係なし」に分けさせる。結果は `~/nanokvm-cve/<日付>/triage.md`、終わったら通知
- 定期実行: launchd `jp.akki.nanokvm-cve-watch`（毎週月曜 9:00）。台本は `~/.local/share/akkijp/nanokvm-os`（akki/os の worktree。毎回 origin に合わせる）から動く。launchd の仕事は `~/Documents` を読めない（TCC）ので、置き場所はどちらも `~/Documents` の外
- 手で回す: `os/cve/watch.sh`（Codex なしは `--no-triage`）。nkbuild へは 1Password の SSH エージェントで入るので、1Password が閉じていると失敗し、そう通知する

2026-11-26 の初回: device はカーネル以外 767 件・rebuild（できあがりの構成）は 287 件（openssh・OpenSSL・expat・vim・rsync・Django・libxml2 は 0 に）。カーネル 5.10.4 は 6,878 件で、Buildroot を上げても減らない。Codex の初回の仕分けは「すぐ対応」0 件。

## できたもの（2026-11-26）

`~/os/images/nanokvm-os-br2025.02.20-app2.5.4.1-20261126.img`（nkbuild、1.69 GB、SHA-256 `d6d7666b3777bb0a8cec6327d92dd2f618db36700b8d704d071daadc1473863b`）。SDK a4d8622・Buildroot 2025.02.20・アプリ 2.5.4.1。

- ルートファイルシステムは e2fsck で誤りなし（使用 581 MB。公式は 776 MB）。起動区画・起動台本・`/kvmapp` は公式と同じ形
- 生成中に出る `rootfs.sd(1677728064) is larger than partition size(1677721600)` は、rootfs.sd の末尾の詰め物の分だけで、ext4（409600 ブロック）は区画にちょうど収まる。SDK のままのビルドでも出る
- まだ機器で起動を確かめていない（下の手順で microSD に書いて確かめる）

### 2024.02 版（比べるため）

2024.02 LTS の最後のリリース 2024.02.13 でも作った（`build.sh ~/os/sdk br2024`。`BR_SERIES=os/buildroot-2024.02`）。2024.02 では zstd・`-ztext` の直しは要らず、numpy の riscv だけ足す。NanoKVM の層は同じ（gdb・bind も外す）。

`~/os/images/nanokvm-os-br2024.02.13-app2.5.4.1-20261126.img`（SHA-256 `3141a35ec806458660535fa462b1057c2f1110e83d9cd6acabff0e7b3f6188cb`）。e2fsck で誤りなし、使用 608 MB、起動台本は公式と同じ 27 本。

| 構成（2026-11-26 の NVD） | カーネル以外の CVE | うち CVSS 7.0 以上 | カーネル 5.10.4 |
|---|---|---|---|
| いま機器に入っている構成（2023.11.2） | 767 | 404 | 6,878 |
| 2024.02.13 | 635 | 314 | 6,878 |
| 2025.02.20 | 287 | 152 | 6,878 |

2024.02 はもう修正が出ないので、openssh・expat・vim・rsync などが残る。使うのは 2025.02 版。見張りは 3 つとも追う（`rebuild2024`）。

### 機器で確かめて直したこと（2026-11-27）

- **SSH が 1 回目のあと受け付けなくなる**: 2025.02 の OpenSSH 9.9p2 には、失敗とみなした接続の送り元をしばらく受け付けない仕組み（PerSourcePenalties、9.8 から既定で有効）がある。この機器では、認証まで成功した接続でも、認証前の処理（seccomp のサンドボックスの中の子）が終了コード 255 で終わり、「異常終了」として送り元に罰が付く（`sshd -d` の記録に `mm_reap: preauth child exited with status 255`）。ルーター越しの接続は送り元が 1 つ（例: 192.168.101.1）に見えるので、2 回目からはそのアドレスの接続を受け取らずに置き去りにし（`CLOSE_WAIT` のまま）、つなぎ直すたびに罰が延びる。公式 1.4.3 の OpenSSH 9.6 にはこの仕組みが無い。直し: post-build で `sshd_config` に `PerSourcePenalties no` を足す（認証の失敗は MaxAuthTries で抑える）
- **ホスト名が `kvm-`・MAC アドレスが欠ける**: SDK a4d8622 の `S10uuid` が、誰も作らない `/device_key_legacy` を読む。公式 1.4.3 の `S10uuid`（チップ固有の `/sys/class/cvi-base/base_uid` から作る）に差し替えた（`os/nanokvm/S10uuid`）。MAC は `48:da:35:6f:` ＋ base_uid の SHA-512 の 4 桁で、台ごとに違う
- **sshd が無い**: Sipeed の defconfig では openssh を mosh が select していたため、最小の defconfig から落ちた。`packages.add` に明示した
- 機器ごとの写し: `os/nanokvm/personalize.sh`（今のカードの p3 の位置・`/etc/kvm`・起動区画の固有ファイルを引き継ぐ）

## microSD に書く・戻す

書く前に、いまの microSD をまるごと写す（これがいちばん確かな戻し方）。

```bash
# 機器の電源を切ってカードを Mac に挿し、ディスク番号を確かめる（間違えると Mac のディスクを消す）
diskutil list
diskutil unmountDisk /dev/diskN
sudo dd if=/dev/rdiskN of=nanokvm-sd-backup.img bs=4m status=progress   # 今のカードを写す
sudo dd if=<作ったイメージ>.img of=/dev/rdiskN bs=4m status=progress     # 新しいイメージを書く
diskutil eject /dev/diskN
```

- 書き込むと `/etc/kvm` の設定（パスワード・TLS・Wi-Fi）も消える。先に `/etc/kvm` を控え、入れ直す
- 戻すときは、写しておいたイメージ（または公式の `20260610_NanoKVM_Rev1_4_3.img`）を同じ手順で書く
- 初回起動ではアプリの移行（`kvm_new_app`）が起動台本を整え、もう一度起動し直す
