# Map import: 変換仕様（#10）

地図データ（OpenStreetMap / GeoJSON）から、編集できる Environment Recipe の**たたき台**を作るときの変換規則です。
実装は `tools/env_map.py`（変換）と `tools/env_polygon.py`（多角形の処理）です。
この文書の表の値は `tests/test_map_spec.py` がコードの定数と照合するので、どちらかだけを変えるとテストが落ちます。

地図データを直接 GLB / MJCF にはしません。いったん普通の Recipe の部品（`building-footprint`、`road-path`）にして、その後は手で置いた部品と同じ生成器・検証を通します（[data-contract.md](data-contract.md)）。

## 1. 入力

| 取得元 | 入り方 | `geo.provider` |
|---|---|---|
| OpenStreetMap | Overpass API（既定 `https://overpass-api.de/api/interpreter`、`HAKONIWA_OVERPASS_URL` / `--endpoint` で変更） | `openstreetmap` |
| OpenStreetMap（保存済み） | Overpass JSON ファイル（`--osm-json`、Studio が保存した `work/map-data/<id>.json`） | `openstreetmap` |
| GeoJSON | FeatureCollection（`--geojson`、Studio の地図ページ）。座標は `[lon, lat]` | `geojson` |

範囲は bbox（南・西・北・東の緯度経度）です。Studio の地図ページでは「地図の中心 ± 東西 / 南北 m の半分」から bbox を計算します。GeoJSON で bbox を省くと、データ全体の範囲（`bbox` があればそれ）を使います。

Overpass のクエリ（`{s},{w},{n},{e}` は bbox）：

```text
[out:json][timeout:80];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]["type"="multipolygon"]({s},{w},{n},{e});
  way["highway"]({s},{w},{n},{e});
);
out body;
>;
out skel qt;
```

制限：範囲は一辺 **2000 m** まで（それを超えると `out_of_range`）。

## 2. 対象にする要素

| 種類 | 条件 |
|---|---|
| 建物 | `building` タグがあり `no` でない。閉じた way、または `type=multipolygon` の relation（`outer` の way をつないで輪にする） |
| 道路 | `highway` タグがあり、下の除外に当たらず、`area=yes` でない way |

除外する `highway`（車の道路ではないもの）：

<!-- excluded-highways -->
`abandoned` `bridleway` `bus_guideway` `bus_stop` `construction` `corridor` `cycleway` `elevator` `escape` `footway` `path` `pedestrian` `platform` `proposed` `raceway` `rest_area` `services` `steps` `track` `via_ferrata`
<!-- /excluded-highways -->

GeoJSON では、`building` プロパティを持つ Polygon / MultiPolygon を建物、`highway` プロパティを持つ LineString / MultiLineString を道路にします。それ以外（Point など）は無視します。

## 3. 座標変換

- 原点：bbox の中心（緯度・経度の中点）。Recipe の `geo.origin` に記録します。
- 変換：原点での局所接平面。WGS84 楕円体の子午線曲率半径 M と卯酉線曲率半径 N を使います。
  - x（東, m）=（経度 − 経度₀）× N × cos(緯度₀) × π/180
  - y（北, m）=（緯度 − 緯度₀）× M × π/180
- 精度：数 km の範囲で 0.1 % 未満（300 m 四方なら端で数 cm）。より広い範囲や測量精度が要る用途には向きません。
- 環境の大きさ：bbox の東西・南北の幅を同じ式で m に直し、mm に丸めます。
- 高さ：地図データの標高は使いません。地面は平ら（既定の地形は `city-ground`）です。

## 4. 形の整理

1. 点を m に直し、**5 cm** より近い点をまとめます。建物では、閉じる点の重複と一直線上の点も取り除きます。
2. **範囲で切り取ります。**
   - 建物：範囲の四角で切り取ります（Sutherland-Hodgman）。
   - 道路：範囲の四角を**道路幅の半分だけ内側に縮めた枠**で切り取ります（Liang-Barsky）。道路の端が環境の外にはみ出さないようにするためです。範囲を出て戻ってくる道路は、別々の部品になります。
3. **取り込まない条件**（取り込みの結果の `skipped` に理由付きで入ります）：
   - 範囲の外にある
   - 建物の面積が **4 m²** 未満
   - 道路の長さが **1 m** 未満
   - 輪郭が不完全（データに無い節点を参照している、閉じていない）
   - 切り取ったあと輪郭が自己交差する
4. **位置と外形**：点の外接箱の中心を部品の位置（`pose`、向き 0）にします。外形・中心線はそこからの相対座標で、mm に丸めます。

## 5. 建物（`building-footprint`）

外形の多角形を、下端から高さまで押し出します（屋根は平ら）。凹んだ外形は凸の部品に分けます（[data-contract.md](data-contract.md) の prism）。

**高さ（`height_m`）**は、次の順で最初に決まったものを使います。

1. `height` タグ（`12`、`12 m`、`12,5m` は m、`40'` や `40 ft` はフィート）
2. `building:levels` × **3 m**。`roof:shape` があって `flat` でなければ、さらに +1 m
3. `building` の値ごとの既定値（下の表。表に無い値は **9 m**）

最終的な高さは 1〜500 m に収めます。

<!-- heights-by-kind -->
| building | 高さ (m) |
|---|---|
| `house` | 6 |
| `detached` | 6 |
| `semidetached_house` | 6 |
| `terrace` | 6 |
| `bungalow` | 4 |
| `residential` | 9 |
| `apartments` | 15 |
| `dormitory` | 12 |
| `hotel` | 20 |
| `commercial` | 12 |
| `office` | 15 |
| `retail` | 6 |
| `supermarket` | 6 |
| `industrial` | 8 |
| `warehouse` | 8 |
| `factory` | 10 |
| `school` | 12 |
| `university` | 15 |
| `hospital` | 18 |
| `public` | 12 |
| `civic` | 12 |
| `church` | 12 |
| `temple` | 8 |
| `shrine` | 6 |
| `garage` | 3 |
| `garages` | 3 |
| `carport` | 3 |
| `shed` | 3 |
| `hut` | 3 |
| `kiosk` | 3 |
| `roof` | 4 |
<!-- /heights-by-kind -->

**下端（`min_height_m`）**は、次の順で最初に決まったものを使います。0 のときは書き出しません。

1. `min_height` タグ
2. `building:min_level` × 3 m
3. `building=roof`（壁の無い屋根：駅のホームの屋根、ガソリンスタンドの屋根など）なら、高さ − **0.5 m**（厚さ 0.5 m の板として浮かせる）

どの場合も下端は「高さ − 0.5 m」を超えません。

multipolygon の中庭（`inner` の輪）は切り抜きません（`notes` に記録）。`building:part` は読みません。

## 6. 道路（`road-path`）

way 1 本（切り取りで分かれたらその各部分）が部品 1 つです。中心線に沿った帯で、曲がり角は埋めます。車線の線も引きます。

- 車線数（`lanes`）：`lanes` タグ。無ければ `highway` の値ごとの既定値（下の表。表に無い値は **2**）。部品としては 1〜**4** に収めます。
- 幅（`width_m`）：`width` タグ。無ければ 車線数 × **3.25 m**。2.5〜60 m に収めます。

<!-- lanes-by-class -->
| highway | 車線数 |
|---|---|
| `motorway` | 4 |
| `trunk` | 4 |
| `primary` | 2 |
| `secondary` | 2 |
| `tertiary` | 2 |
| `unclassified` | 2 |
| `residential` | 2 |
| `living_street` | 1 |
| `service` | 1 |
| `road` | 2 |
| `motorway_link` | 1 |
| `trunk_link` | 1 |
| `primary_link` | 1 |
| `secondary_link` | 1 |
| `tertiary_link` | 1 |
<!-- /lanes-by-class -->

道路は `surface` レイヤーです。道路どうしは交差しても重なり扱いにせず、上に置いた物は道路の上面に立ちます。`bridge`、`tunnel`、`layer` タグは記録するだけで、立体交差は作りません（すべて地面の上）。

## 7. ID と出典

- 部品の ID：
  - `building-<way id>`、`building-r<relation id>`
  - `road-<way id>`。分かれたら `road-<way id>-1`、`road-<way id>-2`、…
  - GeoJSON で `way/123` 形式の id が無いものは、通し番号を使います
  - 重なった ID には `-2`、`-3` を付けます
- 部品の `source`：`provider`、`kind`（way / relation / feature）、`id`、`tags`。残すタグは `building` `highway` `name` `height` `min_height` `building:levels` `building:min_level` `roof:shape` `lanes` `width` `oneway` `surface` `bridge` `layer` です。
- Recipe の `geo`：`provider`、`origin`、`bbox_deg`、`projection`、`data_timestamp`（Overpass の `timestamp_osm_base`）、`query`。OSM の場合はさらに `attribution: © OpenStreetMap contributors`、`license: ODbL-1.0` も入れます。
- Studio で取り込んだときは、取得したデータを `work/map-data/<id>.json` に保存します。同じデータから `--osm-json` で作り直せます。

## 8. 決定性

同じ入力データと同じ bbox からは、同じ Recipe（バイト単位で同じ YAML）ができます。

- 部品は、建物 → 道路の順、同じ種類の中では要素の種類と id の順に並べます。
- 補完はすべてこの文書の固定値です（乱数・現在時刻・外部の状態を使いません）。

Overpass から取り直すと、OSM 側の編集によって結果が変わることがあります（`data_timestamp` で区別できます）。

## 9. できないこと（今後）

- 地形の標高（DEM）
- 屋根の形、テクスチャ
- 中庭の切り抜き、`building:part`（高さで外形が変わる建物）
- 立体交差・橋・トンネル
- 歩道・横断歩道・信号・標識の自動配置
- PLATEAU（CityGML）：実測の 3D 形状・DEM・LOD2 を扱う hakoniwa-envsim の City パイプラインがあるので、この変換では扱いません。

hakoniwa-envsim の建物の物理分類（P0〜P3）と比べると、ここでの建物は **P0 に相当するか、それより粗い**ものです。

- 外形の凹みは保ちます（envsim の P0 は 1 つの向き付きの箱、または外周の壁）。
- 高さは実測ではなく、タグと既定値です。
