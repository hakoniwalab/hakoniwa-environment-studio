# CityGML → 部品：変換仕様（部品変換ツール A）

CityGML の建物・道路を、Environment Studio で編集できる部品にする規則です。
実装は `tools/env_citygml.py` で、地図ページの取り込み（`POST /api/map/import`）もこれを使います。

## 1. 位置づけ

```text
PLATEAU ─────────────────────────────┐
OpenStreetMap / GeoJSON ─ envsim osm2citygml.py ─┴→ CityGML（標準の中間表現。EPSG:6697 / 4326）
                                                         │  envsim の抽出関数で読む
                                                         ▼
                                   env_citygml.py → Recipe（部品）→ Studio で編集・検証・生成
```

- **公開の共通資産（hakoniwa-envsim）**：CityGML の読み書き（座標系、LOD1 の抽出、OSM からの変換）。OSM の変換規則（高さ・幅の補完表など）は envsim の `docs/osm-to-citygml.md` にあります。
- **このリポジトリ（プライベート）**：CityGML を編集できる部品にする部分。

envsim は `$HAKONIWA_ENVSIM_ROOT`、無ければ `../hakoniwa-envsim` から読みます。
依存（hakoniwa-envsim と shapely・trimesh など）は、このリポジトリの Business Pack Recipe `recipes/business-pack/environment-studio.yaml` にあり、Business Pack の Workspace で `recipe.py configure` が Foundation Python に用意します（README の Quick start）。以下の `<ws>` は Studio の Recipe workspace（`$HAKONIWA_WORK_DIR/recipes/environment-studio`）です。

## 2. 入力と選択

- **入力**：CityGML ファイル、または `*bldg*_op.gml`・`*tran*_op.gml` を含むディレクトリ（再帰的に探す）。
  - 同じ内容のファイルは 1 回だけ読みます（envsim のビルドが `build/source/` にコピーを持つため）。
  - 同じ gml:id の建物も 1 回だけ扱います（メッシュの境界で 2 つのファイルに入っている場合）。
- **選択**：中心（緯度・経度）と南北・東西の半分の大きさ（m）。
  - 建物は envsim の規則で選びます。つまり、外形の面積重心が選択範囲に入るものです。
  - 座標は、中心を原点とする envsim の局所 ENU（m）です。

## 3. 部品の単位

**建物 1 棟 = 部品 1 つ**です。単位は CityGML の `bldg:Building`（gml:id）です。

- `BuildingPart` は親の建物に含まれます。
- 外周が分かれている建物は `<id>__part_001`、… の部品になります（envsim の抽出と同じ）。
- 建物は**切りません**。選んだ建物がはみ出す場合は、環境の大きさのほうを、原点を中心に 0.1 m 単位で広げます（`notes` に記録）。

LOD2 の見た目（B-1、4.1 節）と envsim の当たり判定（B-2、7.1 節）は、同じ部品（`source.id` の gml:id）に付け、部品の移動・回転に合わせて一緒に動かします。
そのため、部品の原点（外形の外接箱の中心）を、その建物のローカル原点とします。

## 4. 建物 → `building-footprint`

| 部品のパラメータ | 値 |
|---|---|
| 位置（`pose`） | LOD1 の底面の外形の外接箱の中心（mm に丸める）。向き 0 |
| `footprint` | 底面の外形（その中心からの相対座標、反時計回り、mm）。1 mm より近い重複点と一直線上の点は除く |
| `height_m` | 地面から上端までの高さ（1〜500 m） |
| `min_height_m` | 地面から下端までの高さ。0 のときは書かない |

**地面の高さ**：Studio の地面は平ら（`city-ground`）なので、建物ごとに「地面」を決めます。

- **osm2citygml の出力**（`gen:stringAttribute` の `base_m` がある）：下端 = `base_m`、地面 = 底面の高さ − `base_m`。ホームの屋根のような浮いた構造は、そのまま浮きます。
- **それ以外（PLATEAU の標高）**：その建物の底面を地面とします。高さ = 上端 − 底面、下端は 0。平らな地面では、坂の街の建物ごとの標高差は失われます。City World の地形（DEM）を地面にすれば、建物は地形の上に立ちます（7 章）。

**中庭（穴）**：CityGML の内側のリングは、部品の `holes`（外形と同じ座標）になります。上面図・重なりの検査・（envsim の当たり判定がない建物の）当たり判定で、中庭は空いたままです。外形は、中庭を囲む凸の部品に分けます（制約付き Delaunay 三角形分割を凸を保つ限りつなぐ）。1 m² 未満のもの（採光井戸など）、mm に丸めると形が崩れるもの、外形からはみ出すものは埋めます（結果の `courtyards` と `courtyards_filled` に件数）。重なりの切り取りで中庭が残る・空く場合も、`holes` として保ちます。

### 4.1 LOD2 の見た目（B-1）

ワークスペースの City World から取り込むときは（`--envsim-build`、地図ページの「できたもの」）、LOD2 のある建物ごとに、その建物だけの GLB を `<Recipe 名>.assets/<部品 ID>.glb` に書き、部品の `visual` パラメータで指します（Recipe からの相対パス）。

- **中身**：その建物の LOD2 の面（`lod2MultiSurface`・`lod2Geometry`・`lod2Solid`）を三角形にしたもの。PLATEAU のテクスチャ（写真）はテクスチャ座標ごと埋め込みます。
  - 画像は、City World の `components/buildings/buildings-glb-receipt.json` に記録された場所（ビジネスパックの共有キャッシュなど）から引き当てます。
  - 記録に無ければ、CityGML の隣の画像を使います。
  - どちらも無い面は、単色になります。
- **座標**：glTF の軸（x 東、y 上、z 南向きが負）で、原点は部品の位置（外形の外接箱の中心）と、その建物の地面（LOD1 の底面の高さ）です。Studio が部品を動かす・回すと、見た目も同じ変換で動きます。
- **生成**：生成する GLB（3D プレビューも同じ）では、`visual` のある部品は、押し出した外形の代わりにこの GLB の面を描きます。当たり判定（MuJoCo）は、envsim の当たり判定があればそれ（7.1 節）、無ければ LOD1 の外形です。
- **検査**：`visual` のファイルが読めない、または GLB でなければ、`objects[i].params.visual` の診断になります。GLB の SHA-256 は `resolve` の出力と、生成物の指紋に入ります。
- **読み取り**：envsim の関数（外観の対応表・リングの三角形分割・座標変換）を使い、CityGML はストリームで読みます（沼津の 25 棟で約 11 秒）。
- **使わない場合**：`--no-visuals`（API では `visuals: false`）で LOD1 の箱だけにできます。

## 5. 道路 → `road-area`

- envsim の LOD1 道路面の抽出（`tran:Road` の `lod1MultiSurface`）で読み、選択範囲で切り取ります。道路は部品としての同一性より面の形が大事なので、切り取ります。
- 1 つの面が 1 つの部品です（`<gml:id>-<面の番号>-<切り取りの番号>`）。
- 外形は 2.5 cm で単純化します（道路は surface レイヤーで重なりが問題にならないため。建物の外形は単純化しません）。400 点を超える場合は、さらに粗くします。面積 1 m² 未満は捨てます。
- `road-area` は surface レイヤーです。道路どうしは重なってよく、上に置いた物は道路の上に立ちます。穴（島など）は埋めます。
- LOD2 / LOD3 の道路面（車道・歩道の区別）は今後です。

## 6. ID と出典

- **部品の ID**：gml:id を小文字にし、使えない文字を `-` にしたものです。64 文字を超えるときは、先頭とハッシュにします。重なった ID には `-2`、`-3` を付けます。
- **部品の `source`**：
  - `provider`（`openstreetmap` / `plateau` / `citygml`）
  - `kind: citygml`
  - `id`（gml:id）
  - `note`（元のファイル名）
  - `tags`（`gml:name` と `gen:stringAttribute`。OSM 由来なら OSM の ID・タグ・`height_source` など）
- **Recipe の `geo`**：
  - `origin`（選択の中心）
  - `bbox_deg`（環境の範囲）
  - `projection`（使った座標系）
  - `attribution`（OSM：© OpenStreetMap contributors、PLATEAU：国土交通省）
  - `license`（OSM：ODbL-1.0）
  - `query`（変換器の版と、元ファイルのパス・SHA-256）
  - 地図ページから取り込んだ場合は、さらに `data_timestamp` と Overpass のクエリ

## 7. できたもの（変換済みの City World）

hakoniwa-envsim でビルド済みの街（ビジネスパックの City World ジョブなど）を、そのまま部品にできます。

- **探し方**：`download-manifest.json` のあるディレクトリを、envsim のビルドとして扱います。
  - 探す場所は、地図ページの「フォルダ」、無ければ `HAKONIWA_CITY_WORLD_ROOTS`（PATH と同じ区切り）、どちらも無ければ `../hakoniwa-business-pack/work` です。
  - `foundation`・`downloads`・`cache`・`source`・`components` などには潜りません。
- **一覧**（`GET /api/city-worlds?root=…`）：ジョブ名（`job.json` の `job_id`）、選択範囲（中心・半分の大きさ）、地物の種類、建物数、City World の有無。地図ページでは範囲をオレンジの枠で示します。
- **取り込み**（`POST /api/city-worlds/import`、コマンドは `env_citygml.py --envsim-build DIR`）：
  - ビルドの選択範囲をそのまま使います。
  - 建物は envsim がそのビルドで抽出した外形（`<name>-lod1.json`）を使うので、City World と同じ建物が同じ座標で部品になります。
  - 建物の属性（`gml:name`、`measuredHeight`、`usage` など）は、元の CityGML をストリームで読んで、対象の建物分だけ拾います。数百 MB のメッシュファイルでも数秒です。
  - 道路は、envsim の道路網（7.1 節）を 1 つの層として取り込みます。上面図には元の CityGML の LOD1 面を描きます。
- 取り込むだけで、元のビルドは変更しません。

**地形**：ビルドに地形（`components/terrain/terrain-receipt.json`、DEM あり）があれば、それを地面にします。

- 地面の品目は `city-dem` で、envsim の hfield を**そのまま**使います（格子も高さも同じ。7.1 節）。
- 建物の外形は、その下のいちばん高い地面に立ちます。動かすと、移動先の地面の高さに合わせて上下します。
- 平らな地面にしたいときは、`--flat`（API では `use_dem` を使わない）を指定します。

### 7.1 envsim の出力をそのまま使う（往復で精度を落とさない）

envsim が City World のために作ったものは、Studio で作り直さず、そのまま `<Recipe 名>.assets/` にコピーして使います。**取り込んで何も編集せずに出力すれば、envsim の出力と同じ世界になります。**

| envsim の出力 | Recipe での持ち方 |
|---|---|
| 地形：`terrain.hf`・`terrain.xml`・`terrain.glb` | `assets/terrain/` にコピー。地面 `city-dem` の `dem`（receipt）と `visual`（GLB） |
| 建物ごとの当たり判定：`buildings.xml` の geom（`geom_<gml:id>`・`p1〜p3_surface_<gml:id>_…`・`roof_<gml:id>_…`） | 建物の部品ごとの MJCF `assets/<部品 ID>.xml`。部品の `collision` |
| 道路網などの層：`roads.glb`・`road-markings.glb`・橋（あれば MJCF も） | 層ごとに `city-layer` の物体 1 つ（`visual`、あれば `collision`）。上面図用の外形 `outlines` |

- **置き方**：各物体は `anchor`（資産が作られた位置と、その座標系の envsim の高さ、地形の hfield の SHA-256）を持ちます（[data-contract.md](data-contract.md) 6 章）。
  - 動かしていなければ、資産は envsim の出力と同じ位置に出ます。当たり判定の数値は envsim が書いたまま、envsim の座標系（x 北、y 西）を z 軸まわりに 90°回して置きます。度で書かれた `euler` は、MuJoCo と同じ規則で四元数に直します。
  - 動かすと、資産は部品と一緒に回転・移動し、外形の下の地面の高さの差だけ上下します。複製した建物も、自分の当たり判定（名前に部品 ID を付けて区別）を持ちます。
- **当たり判定と外形**：MuJoCo の世界では、envsim の当たり判定を LOD1 の外形の代わりに使います。Studio の配置と検証（重なり・はみ出し）は、外形で行います。
- **道路網**：envsim の道路は地形に沿わせた見た目だけで、当たり判定は地形そのものです。Studio でも同じく、見た目だけの層（当たり判定なし）にします。1 本ずつ編集したいときは、部品の欄の「個別の道路部品に分解」で、道路の面（`road-area`。地形の上では 10 m のタイル）に置き換えます（元に戻せる操作）。このとき見た目は envsim のものでなくなります。
- **確かめ方**：`tools/env_roundtrip.py --envsim-build BUILD [--recipe R]` が、生成した世界と envsim の `world/city-world.xml`・`city-world.glb` を MuJoCo と頂点で比べます。すべての geom の型・大きさ・接触の設定・位置と向き（1 µm 以内）・メッシュの頂点・hfield のデータが同じで、GLB の地形・道路・建物の頂点が互いに 1 mm 以内にあれば合格です。ワークスペースの 3 つの街（札幌 2 か所・沼津）はすべて合格します（geom の位置の差は最大 6×10⁻¹⁴ m）。
- **使わない場合**：`--no-passthrough`（API では `passthrough: false`）で、従来どおり CityGML からすべて作ります（道路は 10 m のタイルの部品）。

PLATEAU のデータには、外形がもともと重なっている建物もあります（例：札幌で 6.6 m² 食い込む 2 棟）。取り込みのときに、次のように自動で切り離します（`clip_overlaps`）。

- 外形が重なり、高さの範囲も重なる 2 棟では、**小さい方の外形から大きい方と重なる部分を切り取ります**。丸めで再び接しないよう、2 mm 内側に寄せます。
- 部品の位置（見た目の GLB の基準）は変えず、外形だけを差し替えます。見た目（LOD2）はそのままで、当たり判定と上面図が重ならない形になります。
- 切り取った建物の出典に `clipped_by`（相手の gml:id）と `clipped_m2`（切り取った面積）を残し、結果の `clipped` に棟数を出します。
- 屋根と下の建物のように、高さの範囲が重ならない組は切り取りません。
- 切り取ると形が割れる、元の 20 % 未満しか残らない組は、切り取らずに `overlaps_left` で報告します（穴が空く場合は、その穴を `holes` にして切り取ります）。これは人が判断すべきものなので、検証でも重なりとして示します。

## 8. 地図ページからの取り込み

**範囲の指定**は、PLATEAU の City World ブラウザ（ビジネスパックの City World Web UI）と同じ仕様です。

- 入力は Latitude・Longitude と、N/S・E/W の half extent（10〜1000 m）
- 地図のクリック、中心マーカーまたは青い区画のドラッグで位置を変更
- 四隅のハンドルで大きさを変更（反対の角は固定）
- 度とメートルの換算も同じ（緯度 1 度 = 111,320 m。envsim の `plateau_citygml.bounding_box`）
- サーバには `selection: {center: {latitude, longitude}, half_extent_m: {north_south, east_west}}` を送り、入力した値がそのまま選択範囲になります（従来の `bbox` も受け付けます）
- 地面の選択肢は、追加のデータなしで作れる地面だけです（`city-dem` のように City World の地形データが必要な地面は出しません）

`POST /api/map/import` の処理は次のとおりです。

1. Overpass か GeoJSON のデータを、envsim の `osm2citygml.run` で `<ws>/map-data/<id>/map_bldg_op.gml`・`map_tran_op.gml` にします。
2. そのディレクトリを部品変換にかけます。
3. Recipe を `<ws>/recipes/<id>.yaml` に保存します。

元の地図データは `<ws>/map-data/<id>/map.json` に残ります。

## 9. 地図ページから PLATEAU の City World を作る

地図ページの「PLATEAU から City World を作る」で、選んだ範囲の City World を hakoniwa-envsim に作らせ、終わったらそのまま部品として取り込みます（7 章。envsim の出力はそのまま使います）。実装は `tools/env_cityworld.py` です。

- **置き場所**：`<ws>/city-worlds/<ID>/`。ビジネスパックの City World Web UI のジョブと同じ形（`hakoniwa-envsim-build.yaml`、`job.json`、`generation.log`、`build/`）なので、「できたもの」の一覧にも出ます。
- **設定**：City World Web UI と同じ（visual-physics-v1：LOD2 の見た目、建物の当たり判定 P0〜P3、DEM の地形、道路、路面標示、橋）。範囲の指定も同じです。
- **ダウンロード**：envsim が PLATEAU のカタログに問い合わせて CityGML を取ります。取った CityGML は共有のキャッシュに残し、次から使い回します。キャッシュは `HAKONIWA_PLATEAU_CACHE`、無ければ地図ページの「フォルダ」（ビジネスパックの work）の `recipes/city-world-web-ui/runtime/cache/plateau-citygml`、それも無ければ `work/city-worlds/cache` です。
- **実行**：`hako.py --config … build` を裏で動かし、`[HAKO_PROGRESS]` の行から進み具合を表示します（`GET /api/city-worlds/build/<ID>`）。同時に動かすのは 1 つだけで、中止もできます（`POST …/cancel`）。ページを開き直しても、作っている途中のものを追い続けます。
- **オフライン**：同じ ID・同じ範囲で前に作った City World を、そのとき取ったカタログの応答と CityGML だけで作り直せます（`offline`）。前のビルドが無い、または範囲が違うときは、envsim を動かす前に断ります（設定は書き換えません）。範囲を変えるときは、オフラインにせず別の ID で作ります。ダウンロード済みの CityGML は、オフラインでなくても共有キャッシュから使い回します。
- **やり直し**：完成した City World と同じ ID では断ります（上書きは `overwrite`）。途中で失敗したものは、同じ ID でそのままやり直せます。
- **例**：札幌駅前（345 m × 183 m）は、CityGML がキャッシュにあれば十数秒でビルドが終わり、取り込んだ Recipe は `env_roundtrip.py` で envsim の出力と一致します。

