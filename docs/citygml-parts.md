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
依存（shapely・trimesh など）は `requirements.txt` にあり、Workspace Recipe `hakoniwa/recipes/citygml-parts.yaml` で用意できます。

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

LOD2 の見た目や当たり判定（今後の B）は、同じ部品（`source.id` の gml:id）に付け、部品の移動・回転に合わせて一緒に動かします。
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
- **それ以外（PLATEAU の標高）**：その建物の底面を地面とします。高さ = 上端 − 底面、下端は 0。坂の街では、建物ごとの標高差は失われます（DEM の地形を Studio に持ち込むのは今後）。

**中庭（穴）**：Studio の外形は穴を持たないので、埋めた外形になります（`courtyards_filled` に件数）。

## 5. 道路 → `road-area`

- envsim の LOD1 道路面の抽出（`tran:Road` の `lod1MultiSurface`）で読み、選択範囲で切り取ります。道路は部品としての同一性より面の形が大事なので、切り取ります。
- 1 つの面が 1 つの部品です（`<gml:id>-<面の番号>-<切り取りの番号>`）。
- 外形は 2.5 cm で単純化します。400 点を超える場合は、さらに粗くします。面積 1 m² 未満は捨てます。
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

## 7. ワークスペースの街（変換済みの City World）

hakoniwa-envsim でビルド済みの街（ビジネスパックの City World ジョブなど）を、そのまま部品にできます。

- **探し方**：`download-manifest.json` のあるディレクトリを、envsim のビルドとして扱います。
  - 探す場所は、地図ページの「フォルダ」、無ければ `HAKONIWA_CITY_WORLD_ROOTS`（PATH と同じ区切り）、どちらも無ければ `../hakoniwa-business-pack/work` です。
  - `foundation`・`downloads`・`cache`・`source`・`components` などには潜りません。
- **一覧**（`GET /api/city-worlds?root=…`）：ジョブ名（`job.json` の `job_id`）、選択範囲（中心・半分の大きさ）、地物の種類、建物数、City World の有無。地図ページでは範囲をオレンジの枠で示します。
- **取り込み**（`POST /api/city-worlds/import`、コマンドは `env_citygml.py --envsim-build DIR`）：
  - ビルドの選択範囲をそのまま使います。
  - 建物は envsim がそのビルドで抽出した外形（`<name>-lod1.json`）を使うので、City World と同じ建物が同じ座標で部品になります。
  - 建物の属性（`gml:name`、`measuredHeight`、`usage` など）は、元の CityGML をストリームで読んで、対象の建物分だけ拾います。数百 MB のメッシュファイルでも数秒です。
  - 道路は、元の CityGML の LOD1 面から作ります。
- 取り込むだけで、元のビルドは変更しません。

**地形**：ビルドに地形（`components/terrain/terrain-receipt.json`、DEM あり）があれば、それを地面にします。

- 地面の品目は `city-dem` で、envsim の地形をそのまま標本化します。
- 建物は、外形の下のいちばん高い地面に立ちます。動かすと、移動先の地面の高さに合わせて上下します。
- 道路の面は 10 m の格子で切ったタイルにして、それぞれがその下の地面に乗るようにします。1 枚のまま置くと、斜面では最も高い所に合わせて浮いてしまうためです。上面図では、道路の輪郭線は描きません。
- 平らな地面にしたいときは、`--flat`（API では `use_dem` を使わない）を指定します。

PLATEAU のデータには、外形がもともと重なっている建物もあります（例：札幌で 6.6 m² 食い込む 2 棟）。Studio の検証はそれを重なりとして示します。

## 8. 地図ページからの取り込み

**範囲の指定**は、PLATEAU の City World ブラウザ（ビジネスパックの City World Web UI）と同じ仕様です。

- 入力は Latitude・Longitude と、N/S・E/W の half extent（10〜1000 m）
- 地図のクリック、中心マーカーまたは青い区画のドラッグで位置を変更
- 四隅のハンドルで大きさを変更（反対の角は固定）
- 度とメートルの換算も同じ（緯度 1 度 = 111,320 m。envsim の `plateau_citygml.bounding_box`）
- サーバには `selection: {center: {latitude, longitude}, half_extent_m: {north_south, east_west}}` を送り、入力した値がそのまま選択範囲になります（従来の `bbox` も受け付けます）
- 地面の選択肢は、追加のデータなしで作れる地面だけです（`city-dem` のように City World の地形データが必要な地面は出しません）

`POST /api/map/import` の処理は次のとおりです。

1. Overpass か GeoJSON のデータを、envsim の `osm2citygml.run` で `work/map-data/<id>/map_bldg_op.gml`・`map_tran_op.gml` にします。
2. そのディレクトリを部品変換にかけます。
3. Recipe を `work/recipes/<id>.yaml` に保存します。

元の地図データは `work/map-data/<id>/map.json` に残ります。
