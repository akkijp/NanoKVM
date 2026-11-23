# akkijp/NanoKVM（sipeed/NanoKVM のフォーク）

家の NanoKVM（Beta・192.168.150.15）で動かすために、上流のアプリに守りの変更を足したフォーク。
上流は GPL-3.0。このフォークも GPL-3.0 のまま。

## 版の付け方

`上流の版.N`（例: `2.5.4.1`）。上流の `2.5.4` に、このフォークの変更を足した N 回目。

- 枝: `akki/<上流の版>`（例: `akki/2.5.4`）。上流のタグから切る
- タグ: `2.5.4.1` のように付ける
- 上流が新しい版を出したら、新しい枝 `akki/2.5.5` を上流のタグから切り、変更を載せ直して `2.5.5.1` から数える

## 上流からの変更（2.5.4.1）

| 変更 | 理由 | 場所 |
|---|---|---|
| 4 つの数字の版（X.Y.Z.N）を受け付ける | 上の版の付け方のため。上流は 3 つの数字だけを通し、画面は semver で比べるので 4 つだと壊れる | `server/service/application/version.go`、`web/src/lib/version.ts`、`scripts/*.sh`、`.github/workflows/` |
| 更新の取得先を cdn.sipeed.com からこのフォークの GitHub Releases に替える | 中国の CDN に問い合わせない。更新は自分でビルドしたものだけにする | `server/service/application/service.go` |
| ログイン失敗のロックを常に有効にする（既定 900 秒・5 回） | 上流の既定は 0（無効）で、総当たりを止めない | `server/config/default.go` |
| パッケージは版ごとのタグの URL から取り、名前と版の一致を確かめる | 動く「latest」から取ると、確かめた版と違う物が来うる | `server/service/application/version.go` |
| オフライン更新（ファイルを上げる更新）では SHA-256 を必須にし、取得先のリンクをこのフォークにする | 上流の配布物を画面から上げても、確かめずに入ってしまう | `server/service/application/update_offline.go`、`update/offline.tsx` |
| カスタム更新サーバーとプレビューの経路をふさぐ | 古い設定ファイル（`/etc/kvm/application-update.json`）が残っていても、上流や他所から取らない | `server/service/application/update_server.go`、画面の更新設定 |
| 画面の版の比較を作り直す（4 つの数字・rc 版の順・大きな数字・読めない版を「最新」と出さない） | semver の比較では 4 つの数字の版を扱えない | `web/src/lib/version.ts` |
| 保守用の `update-nanokvm.py` を、SHA-512 を確かめて入れる形に書き直す | 上流のものは cdn.sipeed.com の `latest.zip` を確かめずに入れていた。最初の 2.5.4.1 を入れる入口にもなる | `kvmapp/system/update-nanokvm.py` |
| マイクを切る（録音デバイスを持つサウンドカードを起動時に外す） | NanoKVM は録音を使わない。`/etc/kvm/enable-microphone` を置けば切らない | `kvmapp/system/init.d/S95nanokvm` |
| AI 操作機能（picoclaw）の導入と起動を断る | LLM にキーボード・画面を渡す機能で、本体は cdn.sipeed.com から同じ CDN のハッシュで取る | `server/service/picoclaw/fork_disabled.go`、`S95nanokvm` |
| ビルド環境の取得物を確かめる（ツールチェーンと Go の SHA-256、MaixCDK のコミット固定）。`make` のたびに Dockerfile からビルド環境を作る | 上流は sophon.cn・go.dev から確かめずに取り、MaixCDK は main の最新を使う。古いビルド環境が残ると固定が効かない | `docker/Dockerfile`、`Makefile` |
| GitHub Actions と devcontainer を消す | 上流のリリース手順（main・GHCR の `:latest`・上流の CDN）向けで、このフォークでは使わない。ビルドは nkbuild で行う | `.github/workflows/`、`.devcontainer/` |

ツールチェーン（host-tools.tar.gz）の SHA-256 は、2026-11-22 に取ったものを固定した（初めて取った値を信じる形。上流は値を出していない）。

## ビルド

ビルドは家の Proxmox の VM `nkbuild`（pvexf・VMID 214）で行う。上流の手順どおり:

```bash
make release VERSION=2.5.4.1
```

成果物は `build/release/` の `nanokvm_2.5.4.1.tar.gz`・`latest.json`・`sha256.txt` の 3 つ。`scripts/verify-release-assets.sh build/release 2.5.4.1` で確かめる。
ビルド環境は linux/amd64 だけ（ツールチェーンが x86-64 用のため）。

テストは `scripts/fork-test.sh`（docker が要る。nkbuild で回す）。

## リリース

1. `akki/2.5.4` に注釈付きタグ `2.5.4.1` を付けて push する。`scripts/verify-release-tag.sh 2.5.4.1` でタグが `akki/2.5.4` の上にあることを確かめる
2. GitHub Releases の `2.5.4.1` に `nanokvm_2.5.4.1.tar.gz`・`latest.json`・`sha256.txt` を上げる。機器は `releases/latest/download/latest.json` を読み、`releases/download/<版>/<名前>` から取る

## 機器へ最初に入れる（上流の版から）

上流の 2.5.4 までの更新機能は 4 つの数字の名前を拒否し、取得先も上流のままなので、最初の 1 回は手で入れる。

```bash
# 管理 PC から
scp nanokvm_2.5.4.1.tar.gz kvmapp/system/update-nanokvm.py root@<機器>:/root/
# 機器で（まず --dry-run で確かめる）
python3 /root/update-nanokvm.py --file /root/nanokvm_2.5.4.1.tar.gz --sha512 <latest.json の sha512> --dry-run
python3 /root/update-nanokvm.py --file /root/nanokvm_2.5.4.1.tar.gz --sha512 <latest.json の sha512>
```

サービスを止められなければ何も変えずに終わる。新しい版が起動しなければ、前の版に戻して起動し直し、失敗として終わる。
前の版は `/root/old` に残る。戻すときは `/etc/init.d/S95nanokvm stop` のあと `/kvmapp` と `/root/old` を入れ替えて `reboot`。
2 回目からは画面の更新か `python3 /etc/kvm/update-nanokvm.py`（フォークの Releases から取る）で入る。
