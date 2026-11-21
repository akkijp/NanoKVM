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
| ビルド環境の取得物を確かめる（ツールチェーンと Go の SHA-256、MaixCDK のコミット固定） | 上流は sophon.cn・go.dev から確かめずに取り、MaixCDK は main の最新を使う | `docker/Dockerfile` |

ツールチェーン（host-tools.tar.gz）の SHA-256 は、2026-11-22 に取ったものを固定した（初めて取った値を信じる形。上流は値を出していない）。

## ビルド

ビルドは家の Proxmox の VM `nkbuild`（pvexf・VMID 214）で行う。上流の手順どおり:

```bash
make release VERSION=2.5.4.1
```

成果物は `build/release/nanokvm_2.5.4.1.tar.gz` と `latest.json`。
