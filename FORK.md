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
| マイクを切る（録音デバイスを持つサウンドカードを起動・再起動のたびに外す。結果は `/run/nanokvm-microphone`） | NanoKVM は録音を使わない。`/etc/kvm/enable-microphone` を置けば切らない | `kvmapp/system/init.d/S95nanokvm` |
| AI 操作機能（picoclaw）の導入・初期設定・起動を断る（サーバーと起動台本の両方） | LLM にキーボード・画面を渡す機能で、本体は cdn.sipeed.com から同じ CDN のハッシュで取る | `server/service/picoclaw/fork_disabled.go`、`S95nanokvm`、`S96picoclaw` |
| テストとリリースで使うコンテナと pnpm をダイジェスト・ハッシュで固定する | 同じタグでも、VM ごとに違う Node・pnpm でビルドしないため | `scripts/fork-env.sh`、`web/package.json` の `packageManager` |
| ビルド環境の取得物を確かめる（ツールチェーンと Go の SHA-256、MaixCDK のコミット固定）。`make` のたびに Dockerfile からビルド環境を作る | 上流は sophon.cn・go.dev から確かめずに取り、MaixCDK は main の最新を使い、その Python の依存物もその日の最新を入れる（このフォークは `docker/maixcdk-*.lock` でハッシュ固定）。古いビルド環境が残ると固定が効かない | `docker/Dockerfile`、`Makefile` |
| GitHub Actions と devcontainer を消す | 上流のリリース手順（main・GHCR の `:latest`・上流の CDN）向けで、このフォークでは使わない。ビルドは nkbuild で行う | `.github/workflows/`、`.devcontainer/` |

ツールチェーン（host-tools.tar.gz）の SHA-256 は、2026-11-22 に取ったものを固定した（初めて取った値を信じる形。上流は値を出していない）。

## ビルド

ビルドは家の Proxmox の VM `nkbuild`（pvexf・VMID 214）で行う。リリース用の成果物は下の「リリース」の `scripts/fork-release.sh` で作る（中で `make release` を呼ぶ。画面のビルドも固定した Node コンテナで行う）。

成果物は `build/release/` の `nanokvm_2.5.4.1.tar.gz`・`latest.json`・`sha256.txt` の 3 つ。`scripts/fork-release.sh` が置いた `~/releases/2.5.4.1/` を、公開する前に `scripts/verify-release-assets.sh "$HOME/releases/2.5.4.1" 2.5.4.1` で確かめる（fork-release.sh も最後に同じ検査をしている）。
ビルド環境は linux/amd64 だけ（ツールチェーンが x86-64 用のため）。

テストは `scripts/fork-test.sh`（docker が要る。nkbuild で回す）。

## リリース

1. `akki/2.5.4` に注釈付きタグ `2.5.4.1` を付けて push する
2. nkbuild で `scripts/fork-release.sh 2.5.4.1`。タグが `akki/2.5.4` の上にあることを確かめ、タグのコミットを一時的な worktree に取り出してビルドし（普段の作業フォルダには触らない）、成果物を確かめ、最後にタグが動いていないことをもう一度確かめてから `~/releases/2.5.4.1/` に置く（公開はしない）
3. GitHub Releases の `2.5.4.1` に `~/releases/2.5.4.1/` の `nanokvm_2.5.4.1.tar.gz`・`latest.json`・`sha256.txt` を上げる。機器は `releases/latest/download/latest.json` を読み、`releases/download/<版>/<名前>` から取る

## 機器へ最初に入れる（上流の版から）

上流の 2.5.4 までの更新機能は 4 つの数字の名前を拒否し、取得先も上流のままなので、最初の 1 回は手で入れる。上流の画面の更新はこのスクリプトと排他しないので、画面の更新が動いていないことを確かめてから始め、終わるまで画面から更新しない（スクリプトも、アプリが入れ替え途中に見えたら何もせずに止まる）。

```bash
# 管理 PC から
scp nanokvm_2.5.4.1.tar.gz kvmapp/system/update-nanokvm.py root@<機器>:/root/
# 機器で（まず --dry-run で確かめる）
python3 /root/update-nanokvm.py --file /root/nanokvm_2.5.4.1.tar.gz --sha512 <latest.json の sha512> --dry-run
python3 /root/update-nanokvm.py --file /root/nanokvm_2.5.4.1.tar.gz --sha512 <latest.json の sha512>
```

入れ方は上流の画面の更新と同じ（`/kvmapp` を `/root/old` に退避して新しい版と入れ替え、起動し直す。新しい版の初回起動が `/etc` の起動用ファイルを入れ替える）。そのうえで:

- 新しい版を展開・確認して `/kvmapp.new` に置くまでは何も止めない
- 止めた後、2 つのデーモンが消えたことを確かめてから入れ替える。止められなければ何も変えずに終わる。入れ替えに失敗したら前の版を `/kvmapp` に戻して起動する
- 新しい版のデーモンが動き続けなければ、失敗（終了コード 1）として知らせる。入れ替えに失敗して前の版も起動できなかったときも、そう知らせる
- 新しい版の初回起動が HDMI のドライバーを入れ替える機器では、機器がそのまま再起動する（SSH は切れる）。戻ってきたら `cat /kvmapp/version` と `pidof NanoKVM-Server && pidof kvm_system`（両方が動いていること）で確かめる
- 前回の更新が入れ替えの途中で止まっていた（`/kvmapp` が欠けている・`/root/old` が途中まで）ときは何もしない。`/kvmapp` 自体が無く、`/root/old` に `version`・`server/NanoKVM-Server`・`kvm_system/kvm_system` が揃っているときに限り、`mv /root/old /kvmapp` で前の版を戻して再起動してよい。それ以外（`/kvmapp` が残っている・`/root/old` が途中まで）は、下の microSD の書き直しで戻す
- マイクを切れなかったときは、新しい版は動いたまま失敗（終了コード 1）として知らせる（`/run/nanokvm-microphone` に理由）

自動では戻さない。戻すときは、microSD に公式の NanoKVM イメージを書き直す（アプリも `/etc` も工場出荷の状態に戻る）。そのあと必要なら、上の手順でもう一度入れる。

2 回目からは画面の更新か `python3 /etc/kvm/update-nanokvm.py`（フォークの Releases から取る）で入る。
