# AI authoring contract（#9）

AI エージェントが Environment Studio を外から扱うための約束事です。チャット UI ではなく、
`tools/envstudio.py` の JSON 入出力と Studio の JSON API を「道具」として使います。
人が Studio で編集した Recipe と AI が作った Recipe は、同じスキーマ・同じ検証を通ります。
データそのものの約束（Type / Catalog / Recipe、座標、地形、診断）は [data-contract.md](data-contract.md) を見てください。

## 1. 基本ルール

- 常に `--json` を付ける。入力は YAML でも JSON でもよく、`-` で標準入力（そのときは `--base` で catalog パスの基準フォルダ）。
- 終了コード：0 = OK、1 = 入力に問題あり（診断を出力）、2 = 使い方の誤り。
- 座標は ENU（x = 東、y = 北、z = 上）、単位は m と度、原点は環境の中心、yaw は東から反時計回り。
- 同じ入力からは同じ出力（resolve・inspect・generate の指紋）。乱数は地形の `seed` だけで、既定値も固定です。
- 推測しない：スキーマにないフィールドは `unknown_field` として必ず返り、黙って無視されることはありません。

## 2. コマンド

| コマンド | 使いどころ |
|---|---|
| `contract` | 最初に一度。契約・スキーマのバージョン、コマンド、座標系、診断コードの一覧、終了コード |
| `types` / `describe-type <type>` | 使える部品の型と、そのパラメータ（kind・unit・範囲・既定値・どのレベルで決めるか） |
| `catalog <catalog.yaml>` / `describe-item <catalog.yaml> <item>` | 置ける品目と、配置ごとに変えてよいパラメータ（`placement_params`）、解決済みの形と大きさ |
| `validate <recipe>` | スキーマ → MuJoCo の順に検証。`{ok, diagnostics}` |
| `resolve <recipe>` | 解決済みの環境（地形、各物体の z と形状） |
| `inspect <recipe>` | 各物体の範囲（`bounds_m`）、足元の地面、最も近い物体とのすき間（`nearest.gap_m`、0 は接触か重なり）、各端までの余裕（`room_m`、負ははみ出し） |
| `generate <recipe> --out-dir DIR` | `environment.glb` / `.xml` / `.json`（指紋付き） |
| `tools/env_citygml.py (--citygml DIR --center LAT,LON --half-extent NS,EW \| --envsim-build DIR) --out R.yaml --json`、`--list ROOT` | CityGML（PLATEAU、または envsim の `osm2citygml.py` が地図データから作ったもの）から、建物・道路を部品にした Recipe を作る（#10）。結果は `{buildings, roads, skipped, notes, size_m}` |

Studio を起動していれば、同じことを HTTP でもできます（`/api/catalogs/<id>`、`/api/resolve`、`/api/terrain`、`/api/validate`、`/api/glb`、`/api/poses`、`PUT /api/recipes/<id>`、`DELETE /api/recipes/<id>`（Recipe workspace の `trash/` へ移す。例は 403））。
`/api/validate` は問題があっても HTTP 200 で `{ok, stage: "schema" | "physics", diagnostics}` を返します。

## 3. 診断

```json
{"severity": "error", "path": "objects[3].item", "code": "unknown_reference",
 "expected": ["box-obstacle", "landing-pad", "..."], "actual": "helipad", "reason": "not an object item of the catalog"}
```

- `path` は Recipe 内の場所（`objects[i].pose.x_m` など）。`related` はもう一方の物体（重なり）。
- `expected` は直し方の手がかり：許されるフィールド名・選択肢・範囲。
- 一度の検証で見つかった問題はすべて返ります。スキーマの問題があるうちは MuJoCo の検証は行いません（2 段階）。
- 物理の診断：`overlap`（`actual` はめり込み量 m）、`outside`（`actual: {edge, depth_m}`）、`below_terrain`、`compile_error`。許容差は 1 mm。

## 4. generate → validate → repair のループ

```text
User: 20m x 30m のドローン練習場を作って。中央にゲート3つ、右奥に着陸台。
  ↓ contract / catalog / describe-item で使える品目と大きさを知る
AI: Recipe を書く                         tests/fixtures/ai-loop/1-draft.yaml
  ↓ validate → unknown_field / out_of_range / unknown_reference
AI: expected を見て直す                    tests/fixtures/ai-loop/2-draft.yaml
  ↓ validate → outside（depth_m 0.5）/ overlap（related）
AI: inspect で余裕とすき間を見て動かす     tests/fixtures/ai-loop/3-repaired.yaml
  ↓ validate → ok
generate → Studio で人が微調整（同じ Recipe を開いて保存）
```

「右奥」は北東（x > 0、y > 0）。物体の大きさは `describe-item` の `envelope` と `height_m`、置ける範囲は環境の ±半分から大きさの半分を引いたところです。

このループは AI なしで再現できるよう、`tests/test_ai_contract.py` が各段階の診断コードと、修正後の検証 OK・生成の決定性を確かめています。
