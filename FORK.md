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

送るスクリプトは、手元の作業ツリーのものではなく、確かめたパッケージの中のものを使う（別のブランチの古い・上流の版を送らないため）。

```bash
# 管理 PC で: パッケージの SHA-512 を latest.json と照合し、合ったときだけスクリプトを取り出して送る。
# 照合に失敗したら（取れない・空・不一致）tar より前に止まる
(
  set -euo pipefail
  want=$(python3 -c 'import json;print(json.load(open("latest.json"))["sha512"])')
  got=$(openssl dgst -sha512 -binary nanokvm_2.5.4.1.tar.gz | base64 | tr -d '\n')
  if [ -z "$want" ] || [ "$want" != "$got" ]; then echo "SHA-512 が一致しない: 止める" >&2; exit 1; fi
  tar -xzf nanokvm_2.5.4.1.tar.gz nanokvm_2.5.4.1/system/update-nanokvm.py
  scp nanokvm_2.5.4.1.tar.gz nanokvm_2.5.4.1/system/update-nanokvm.py root@<機器>:/root/
)
# 機器で（まず --dry-run で確かめる）
python3 /root/update-nanokvm.py --file /root/nanokvm_2.5.4.1.tar.gz --sha512 <latest.json の sha512> --dry-run
python3 /root/update-nanokvm.py --file /root/nanokvm_2.5.4.1.tar.gz --sha512 <latest.json の sha512>
```

入れ方は上流の画面の更新と同じ（`/kvmapp` を `/root/old` に退避して新しい版と入れ替え、起動し直す。新しい版の初回起動が `/etc` の起動用ファイルを入れ替える）。そのうえで:

- 新しい版を展開・確認して `/kvmapp.new` に置くまでは何も止めない
- 止めた後、2 つのデーモンが消えたことを確かめてから入れ替える。止められなければ何も変えずに終わる。入れ替えに失敗したら前の版を `/kvmapp` に戻して起動する
- 新しい版のデーモンが動き続けなければ、失敗（終了コード 1）として知らせる。入れ替えに失敗して前の版も起動できなかったときも、そう知らせる
- 新しい版の初回起動が HDMI のドライバーか起動台本（S95nanokvm）を入れ替えるときは、機器がそのまま再起動する（SSH は切れる）。上流の版からの最初の導入は起動台本が変わるので必ず再起動する（古い起動台本のまま新しい版を起動すると、マイクと picoclaw を切る処理が走らないため）。戻ってきたら、両デーモンの起動から 15 秒以上たってから次を確かめる: `cat /kvmapp/version` が入れた版、`pidof NanoKVM-Server && pidof kvm_system`（両方が動いている）、`cat /run/nanokvm-microphone /run/nanokvm-picoclaw` がどちらも `off`（`/etc/kvm/enable-microphone` を置いていなければ）、`/etc/kvm/update-restart-pending` が無い。どれかが違えば、更新は済んでいない（下の記録の節を見る）
- 前回の更新が入れ替えの途中で止まっていた（`/kvmapp` が欠けている・`/root/old` が途中まで・記録が `switching`）ときは何もしない。下の「途中で止まったときの戻し方」で戻す
- マイクを切れなかったときは、新しい版は動いたまま失敗（終了コード 1）として知らせる（`/run/nanokvm-microphone` に理由）

### 既知の残り（2026-11-25 のレビューで見送った低いもの）

- 作業フォルダ `/root/.kvm-update` を `/root/old` と別のファイルシステムに置くと、2 回目の更新で前の控えの移動（`rename`）が `EXDEV` で失敗する（何も入れずに止まる）。既定の置き方（どちらも `/root`）では起きない
- S95 のロックのテストは、ロック中に起動が呼ばれないことは見るが、停止が呼ばれないことまでは見ていない

### 「まだ動いたと確かめていない」記録（`/etc/kvm/update-restart-pending`）

画面の更新も `update-nanokvm.py` も、`/kvmapp` を入れ替える前にこの記録を書く（`{"from": 前の版, "to": 新しい版, "phase": "switching"}`）。入れ替えが済んだら `"installed"` に書き換える。記録がある間は `/root/old` が「動いた実績のある版」なので、どちらの更新も何も入れない。

- 新しい版の S95 は、2 つのデーモンが 15 秒動き続けたら記録を消す
- `installed` のまま再起動に失敗したときは、画面の「再起動をやり直す」か `/kvmapp/system/init.d/S95nanokvm restart`
- `switching` のまま止まったとき（入れ替えの途中で電源が落ちた等）や記録が読めないときは、S95 も新しい版の移行処理（`kvm_system --migrate`）も KVM を起動しない（`/kvmapp` が新旧まじりかもしれず、移行処理が `/etc` を書き換えてしまうため）。下の「途中で止まったときの戻し方」で戻す
- 前の版（`from`）か新しい版（`to`）が `/kvmapp` にそろって 2 つのデーモンが動いていれば、`update-nanokvm.py` が記録を消して先へ進む（上流の S95 で前の版が起動し直した場合など）

### 途中で止まったときの戻し方

最初の導入（上流の版から）の途中で止まった場合、`/etc` にはまだ上流の起動台本が残っていることがある。上流の起動台本は未完了の記録を確かめず、マイクも切らない（このフォークのサーバーと移行処理は、記録を見て起動を断る）。戻すまでの間はマイクが有効かもしれないものとして扱う。

1. SSH で入る。SSH を切っている機器（`/etc/kvm/ssh_stop` がある）は、microSD のブート領域（FAT）に空のファイル `start_ssh_once` を置いて起動すると、その起動のときだけ SSH が有効になる
2. `cat /etc/kvm/update-restart-pending` で `from`（前の版）を見る
3. `/kvmapp` 自体が無く、`/root/old/version` が `from` と同じで、`/root/old/server/NanoKVM-Server` と `/root/old/kvm_system/kvm_system` があるときに限り、手で戻せる:

   ```bash
   mv /root/old /kvmapp && sync && rm /etc/kvm/update-restart-pending && sync && reboot
   ```

   （1 つでも失敗したらそこで止まる。`mv` が失敗したら記録を消さずに、microSD の書き直しへ）

4. それ以外（`/kvmapp` が残っている＝画面の更新がファイル単位で動かしていた、`/root/old` が途中まで、版が合わない）は、microSD を書き直す

自動では戻さない。全部を戻すときは、microSD に公式の NanoKVM イメージを書き直す（アプリも `/etc` も工場出荷の状態に戻る）。そのあと必要なら、上の手順でもう一度入れる。

2 回目からは画面の更新か `python3 /etc/kvm/update-nanokvm.py`（フォークの Releases から取る）で入る。
