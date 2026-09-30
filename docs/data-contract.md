# Data contract: Type / Catalog / Recipe

Environment Studio の入力はすべて YAML（JSON でも可）の 3 層です。人が手で書いても、AI が生成しても、Studio が保存しても同じ契約で検証・解決されます（#2）。

| 層 | スキーマ | 置き場所 | 中身 |
|---|---|---|---|
| Type | `hakoniwa.environment-types/v1` | `types/*.yaml`（ツールに同梱、普遍） | パラメータの定義、振る舞い、形状（または地形）の作り方 |
| Catalog | `hakoniwa.environment-catalog/v1` | `catalogs/<id>/catalog.yaml` | 品目＝Type とパラメータの値 |
| Recipe | `hakoniwa.environment-recipe/v1` | `recipes/*.yaml` | 1 つの環境：大きさ、地形、物体の配置 |

設計の出どころは Booth Studio（`hakoniwa-booth-studio/docs/design-part-types.md`）です。違いは、単位（m）、原点（中心）、形状の傾き、くさび形、地形、そして診断（全部の問題を機械可読で返す）です。

## 1. 座標と単位

- 長さは **m**（小数、精度は mm まで）、角度は **度**。
- **ENU**：x = 東、y = 北、z = 上。
- **原点は環境の中心**。大きさ `size_m: {east, north}` は全体の幅で、範囲は ±半分。hakoniwa-urban-mobility の世界（MuJoCo の原点が中心、`half_extent_m`）とそのまま対応します。
- **yaw** は東から反時計回り（urban-mobility の `yaw_deg` と同じ）。
- 物体の向き：物体のローカル +y が「正面」。車はスロープをローカル +y へ登り、ドローンはゲートをローカル y 方向に通り抜け、標識・信号はローカル +y を向きます。

## 2. Type

```yaml
schema: hakoniwa.environment-types/v1
types:
  - id: gate
    extends: object                 # object: 地面に立つ、吸い付かない、摩擦 1.0
    label: ゲート
    description: A frame to fly through along its y.
    id_prefix: gate
    params:
      opening_width_m: {kind: length, label: 開口の幅, default: 1.5, min: 0.3, max: 20, level: placement}
      clearance_m: {kind: length, label: 開口の下端, default: 0.0, min: 0, max: 20, level: placement,
                    description: Height of the opening's bottom above the ground.}
      bar_m: {kind: length, label: 枠の太さ, default: 0.1, min: 0.02, max: 1, level: item}
    shapes:
      - {name: post-left, primitive: box, w: $bar_m, d: $bar_m, h: $clearance_m + $opening_height_m + $bar_m,
         x: -($opening_width_m + $bar_m) / 2}
      - {name: bottom, primitive: box, w: $opening_width_m, d: $bar_m, h: $bar_m,
         z: $clearance_m - $bar_m / 2, when: $clearance_m >= $bar_m}
```

| 項目 | 内容 |
|---|---|
| `id` / `label` / `description` | 識別子、表示名、説明（`describe-type` で人と AI が読む） |
| `extends` / `abstract` | 単一継承。抽象の型は品目から直接使えない |
| `params` | `kind`（length / angle / number / integer / color / enum / bool / text / polygon / polyline）、`unit`（length は m、angle は deg が既定）、`label`、`description`、`default`、`min` / `max` / `values`、`level` |
| `behavior` | `surface`（`ground`：地面に立つ／`elevated`：地面から `z_m` の高さ）、`snap`、`friction`、`layer`（`object` が既定／`surface`：道路や標示。surface どうしは重なってよい） |
| `shapes` | 形状（3 章）。地形の型では代わりに `terrain` |
| `terrain` | `kind`（flat / hfield）、`generator`、`color`、`friction`（4 章） |
| `envelope` | 上面図の外形（既定は形状全部を収める中心合わせの箱） |

子の型で `params: {name: null}` と書くと、親のパラメータを外します。

`polygon`（建物の外形など）と `polyline`（道路の中心線など）は、物体のローカル座標（m）の点の並び `[[x, y], ...]` です。polygon は 3 点以上で、辺が交差・接触してはいけません（`invalid_shape`）。向きは問わず、反時計回りにそろえて保存します。閉じる点の重複や一直線上の点は取り除きます。点は最大 2000 個です。

### 2.1 パラメータの level

| level | 決める人 |
|---|---|
| `type` | 型（固定、既定値が必須） |
| `item` | 品目（Catalog） |
| `placement` | 配置（Recipe）。品目の値が既定値で、品目は範囲・選択肢を狭められる（`{default, min, max, values}`） |

### 2.2 式

形状・振る舞い・地形の値には `$パラメータ` を使った式を書けます。数値、文字列、`+ - * /`、括弧、比較、`and / or / not`、関数 `min`・`max`・`abs`・`sqrt`・`sin`・`cos`・`tan`（**度**）・`cond(条件, 真, 偽)`。Python の `ast` で読み、許可した節点だけを自前で評価します（`eval` は使いません）。`$` のない文字列は式ではなく文字どおりの値です。カンマを含む式は YAML の flow 表記では引用符で囲みます。

## 3. 形状（solid）

| 項目 | 内容 |
|---|---|
| `primitive` | `box`、`cylinder`（直立、`w` = `d` = 直径）、`wedge`（くさび：底面 `w × d`、ローカル +y に向かって 0 から `h` まで上がる）、`prism`（`points` の多角形を `h` だけ押し出す）、`ribbon`（`points` の折れ線に沿った幅 `w` の帯） |
| `points` | prism / ribbon の点（`$footprint` のような polygon / polyline のパラメータ） |
| `w` / `d` / `h` | 大きさ（m） |
| `x` / `y` / `z` | **形状の中心**（物体の底面の中心からの位置）。`z` の既定は `h / 2`（底面に立つ） |
| `roll` / `pitch` / `yaw` | 中心まわりの傾き（度。yaw → pitch → roll の順に適用） |
| `color` / `collide` / `visible` / `when` | 色、衝突に加わるか、見えるか、作る条件 |

形状は物体の底面より下に出てはいけません（出ると `invalid_shape`）。形状の名前は型の中で一意です。

- **prism**：MuJoCo はメッシュを凸包で衝突させるため、凹んだ外形（L 字の建物など）は凸の部品に分けます（耳切りで三角形に分け、凸を保つ限りつなぐ Hertel-Mehlhorn 法。決定的）。部品の名前は `<名前>-1`、`<名前>-2`、…（凸なら `<名前>` のまま）です。傾けられません（点のほうを動かします）。
- **ribbon**：線分ごとの箱 `<名前>-1`、… に分けます。`x` = 0（線の上）のときは、曲がり角を直径 `w` の円柱 `<名前>-joint-1`、… で埋めます。`x` は進行方向の右へのずれです（車線の線など）。

`layer: surface` の物体の形状は、MuJoCo で contype 2・conaffinity 1 になります。ほかの物体や地面とは衝突し、surface どうしは衝突しません（交差点で道路が重なるため）。地面に立つ物体は、足元にある surface の物体（道路）の**上面に**乗ります。道路に置いたコーンは道路にめり込みません。

## 4. 地形

地形は物体ではなく、環境全体の地面です。Recipe の `terrain` に地形の品目を 1 つ指定します。

| kind | 内容 |
|---|---|
| `flat` | z = 0 の平らな地面 |
| `hfield` | 高さの格子（MuJoCo hfield）。`generator` がパラメータから作る |

`hills` 生成器は `max_height_m`・`hills`（丘の数）・`radius_m`・`seed`・`resolution_m`（格子の間隔）を読み、乱数の種で決まる場所にガウス型の丘を置きます。**同じパラメータからは必ず同じ地形**ができるので、Recipe に格子データを持たずに済み、AI も再現できます。

`envsim` 生成器は、hakoniwa-envsim がビルドした City World の地形（PLATEAU の DEM から作った hfield）を読みます（地面の品目 `city-dem`、型 `dem_terrain`）。

- `dem`：その `components/terrain/terrain-receipt.json`（絶対パスか Recipe からの相対パス）
- `resolution_m`：格子の間隔（既定 1 m。格子が 1001 点を超えるときは粗くする）

高さは envsim 自身の読み取り関数で標本化し、いちばん低い点を 0 にします。DEM の範囲の外（環境を広げた分）は、端の高さを延ばします。receipt の SHA-256 と地形ファイルが食い違うときは `invalid_shape` です。

格子の並びは MuJoCo と同じです：行 0 が北の端（+y）、最後の行が南の端、列 0 が西の端（-x）、最後の列が東の端（`tests/test_env_generate.py` で MuJoCo と照合）。

物体は、外形の下（角・辺・内側の格子点）で**いちばん高い地面**に底面を置きます。坂の上でも地面にめり込みません。hfield では、さらに 5 mm 上に置きます（`HFIELD_CLEARANCE_M`）。標本化した高さ（双一次補間、辺は格子の間隔ごと）は、MuJoCo の三角形の面より数 mm 低くなることがあるためです。`elevated` の物体はそこから `z_m` 上です。

## 5. Catalog

```yaml
schema: hakoniwa.environment-catalog/v1
catalog: {id: starter, name: スターター（車・ドローンの試験場）}
items:
  - id: race-gate
    type: gate
    name: レースゲート（1.5 m 角）
    params: {opening_width_m: 1.5, opening_height_m: 1.5, clearance_m: 0.5}
  - id: rolling-hills
    type: hills_terrain
    name: なだらかな丘
    params: {max_height_m: 1.5, hills: 6, radius_m: 5.0, seed: 7}
```

`extends`（同じ Catalog の前の品目を引き継ぐ）、`description`、`category`、`source`（出典）、`assumed`（出典にない値：パラメータ名 → 理由）を持てます。

## 6. Recipe

```yaml
schema: hakoniwa.environment-recipe/v1
name: ドローン練習場（20 m × 30 m）
catalog: ../../catalogs/starter/catalog.yaml     # Recipe ファイルからの相対パス
size_m: {east: 20, north: 30}
terrain: {item: grass-ground}                    # 地形の品目（params も書ける）
objects:
  - {id: gate-1, item: race-gate, pose: {x_m: 0, y_m: -6, yaw_deg: 0}}
  - {id: landing-pad, item: landing-pad, pose: {x_m: 7, y_m: 12, yaw_deg: 0}, params: {color: "#2f6fb0"}}
```

地図や CityGML から作った Recipe（#10、変換の規則は [citygml-parts.md](citygml-parts.md)）は、出典を持ちます。どちらも世界の形は変えず、そのまま保存・出力（`resolve`、`environment.json`）されます。

- `geo`：`provider`、`origin {lat_deg, lon_deg}`（原点 = 環境の中心）、`bbox_deg {south, west, north, east}`、`projection`、`attribution`、`license`、`data_timestamp`、`query`
- 物体の `source`：`provider`、`kind`（way / relation / feature）、`id`、`tags`

`pose` は `x_m`・`y_m`・`yaw_deg` だけです。高さは地形から決まり（4 章）、`elevated` の物体は `params.z_m` で持ち上げます。`params` は型が `placement` とした値だけ書けます。

## 7. 診断（Diagnostics）

検証は、見つけた問題を**全部まとめて**返します。1 件ずつ直して何度も検証し直す必要はありません。

```json
{"ok": false, "diagnostics": [
  {"severity": "error", "path": "objects[1].item", "code": "unknown_reference",
   "reason": "not an object item of the catalog", "expected": ["race-gate", "..."], "actual": "race-gat"}
]}
```

| 項目 | 内容 |
|---|---|
| `path` | 問題の場所（`objects[3].params.width_m`、`terrain.item`、`size_m.east` など） |
| `code` | 種類（下表）。プログラムや AI はこれで分岐する |
| `expected` / `actual` | 期待する値（範囲・候補の一覧）と実際の値。typo には正しい候補の一覧が入る |
| `reason` | 人向けの説明 |

| code | 意味 |
|---|---|
| `unknown_field` | スキーマにない項目（typo のことが多い） |
| `missing_field` | 必須の項目がない |
| `wrong_type` | 値の種類が違う |
| `out_of_range` | 範囲外の数値 |
| `not_one_of` | 選択肢にない値 |
| `unknown_reference` | 存在しない型・品目・パラメータ |
| `duplicate_id` | id の重複 |
| `not_allowed` | その層では変えられない値（型や品目が固定） |
| `invalid_expression` | 読めない・評価できない式 |
| `invalid_shape` | 大きさ 0 の形状、底面より下に出る形状など |
| `wrong_schema` | スキーマのタグがない・版が違う |
| `overlap` / `outside` / `below_terrain` / `compile_error` | 物理検証（MuJoCo、#6） |

### 7.1 物理の検証（MuJoCo、#6）

スキーマの検証を通った Recipe は、MuJoCo で物理の世界として検証します（`tools/env_validate.py`）。検証用の世界では物体を自由な body にし、環境の四辺のすぐ外に固定の壁を置いて、`mj_forward` を 1 回計算して接触を読みます。1 mm までの接触は「接している」だけで問題にしません。

| code | 接触 | `actual` |
|---|---|---|
| `overlap` | 物体と物体 | めり込みの深さ（m）。相手の物体は `related` に `objects[j]` |
| `outside` | 物体と四辺の壁 | `{edge: north / south / east / west, depth_m}` |
| `below_terrain` | 物体と地面 | めり込みの深さ（m） |
| `compile_error` | MuJoCo が読めない | MuJoCo のメッセージ |

```json
{"severity": "error", "path": "objects[0]", "code": "overlap", "reason": "a and b penetrate each other by 100 mm",
 "expected": "<= 0.001 m", "actual": 0.1, "related": ["objects[1]"]}
```

## 8. コマンド

`tools/envstudio.py`（`--json` で JSON 入出力、`-` で標準入力）：

```bash
python tools/envstudio.py types
python tools/envstudio.py describe-type gate
python tools/envstudio.py catalog catalogs/starter/catalog.yaml
python tools/envstudio.py describe-item catalogs/starter/catalog.yaml race-gate
python tools/envstudio.py validate recipes/examples/drone-practice-field.yaml   # スキーマ＋MuJoCo（--no-physics で物理を省く）
python tools/envstudio.py --json resolve recipes/examples/hills-field.yaml
python tools/envstudio.py generate recipes/examples/car-test-course.yaml --out-dir build/car-test-course
```

終了コードは 0 = OK、1 = 入力に問題あり（診断を出力）、2 = 使い方の誤り。AI 向けの使い方は [ai-contract.md](ai-contract.md)（#9）。
