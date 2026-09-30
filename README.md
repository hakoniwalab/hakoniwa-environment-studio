# Hakoniwa Environment Studio

Hakoniwa Environment Studio は、Car / Drone / Robot などを動かすための **シミュレーション環境をブラウザで設計する商用オーサリングツール**です。

専門的な 3D CAD や MuJoCo XML を直接編集しなくても、

1. Catalog から環境部品を選ぶ
2. 2D で配置・編集する
3. 3D で確認する
4. MuJoCo で物理的に検証する
5. Environment Recipe / GLB / MuJoCo World を生成する
6. 生成した Environment を Hakoniwa のシミュレーションで利用する

という流れを、軽量な Studio UI で完結させることを目指します。

## Concept

基本設計は [hakoniwa-booth-studio](https://github.com/hakoniwalab/hakoniwa-booth-studio) で確立した考え方を踏襲します。

```text
Environment Types
      ↓
Environment Catalog
      ↓
Browser 2D Compose
      ↓
Environment Recipe
   ↙            ↘
GLB / Three.js   MuJoCo World
   ↘            ↙
      Validation
          ↓
   Environment Asset
          ↓
hakoniwa-urban-mobility
          ↓
 Car / Drone / Scenario
```

Environment Studio 自身は Car や Drone の制御・シミュレーションを実装しません。

責務は次のように分離します。

```text
Environment = 動かない世界
Actor       = Car / Drone / Robot
Scenario    = route / mission / control / disturbance
```

Environment Studio は **Environment の authoring** を担当し、Actor / Scenario / runtime は Hakoniwa の各コンポーネントへ委譲します。

## Design principles

### 1. Type / Catalog / Recipe

Environment を 3 層に分離します。

- **Type** — 部品の構造、パラメータ、物理属性、形状生成ルール
- **Catalog Item** — Type の具体的な値やプリセット
- **Recipe / Placement** — 実際の環境での位置・姿勢・配置パラメータ

Type の追加だけで新しい環境部品を増やせる構造を優先し、Studio 本体への個別ハードコードを避けます。

### 2. 2D first

主なレイアウト編集は 2D 上面図で行います。

- drag / rotate
- grid snap / align
- duplicate / delete
- multi-select
- undo / redo
- parameter editing

3D は、配置後の見え方・高さ・地形・障害物の確認を主用途とします。

### 3. One Recipe, multiple outputs

同一の Environment Recipe から、

- Three.js 用 GLB
- MuJoCo World
- Environment metadata / asset information

を生成します。

表示用モデルと物理用モデルで別々の手入力を持たず、同じ解決済みデータから生成することで整合性を保ちます。

### 4. MuJoCo as the physical validation backend

配置や形状を独自ロジックだけで判定せず、MuJoCo を正式な物理 validation backend として利用します。

検証結果は Human と AI の双方が扱えるよう、可能な限り machine-readable diagnostics として返します。

### 5. AI-first extensibility

本 Studio の重要な差別化軸は、単に「AI 機能を付ける」ことではありません。

**AI が Type / Catalog / Recipe を安全に生成・検証・修正しやすい、自己記述的でデータ駆動な構造**を設計の中心に置きます。

```text
Human or AI
    ↓
Type / Catalog / Recipe
    ↓
Schema validation
    ↓
Resolve
    ↓
GLB / MuJoCo
    ↓
Physical validation
    ↓
Machine-readable diagnostics
    ↺
```

目標は、たとえば次のような要求を AI が Recipe へ落とせることです。

> 20m × 30m のドローン練習場を作る。中央にゲートを3つ置き、右奥に着陸台を配置する。

AI チャット UI 自体は MVP の必須条件ではありません。
まず、AI が安定して扱える schema / validator / resolver / generator の契約を優先します。

## Terrain

MVP では地面を次の2系統から始めます。

- flat ground
- MuJoCo `hfield` terrain

凹凸地面は primitive の集合で近似せず、`hfield` を正式な terrain 表現とします。

## Starter Catalog

最初から道路・都市設備を網羅することは狙いません。
Car / Drone の実験場をすぐ作れる最小 Catalog から始めます。

### Terrain

- flat ground
- hfield terrain

### Structure / obstacle

- wall
- building block
- box obstacle
- pylon
- gate
- ramp / slope
- platform / landing pad

### Roadside

- road surface / lane marking
- stop line / stop marking
- guard rail
- basic road sign
- traffic signal

道路標識や信号は、まず外観・配置・collision を扱います。
信号制御や交通ルールは Scenario 側の責務とします。

## Relationship with Hakoniwa Urban Mobility

Environment Studio の生成物は、最終的に Environment Asset として
[hakoniwa-urban-mobility](https://github.com/hakoniwalab/hakoniwa-urban-mobility)
から利用できることを目標とします。

```text
Hakoniwa Environment Studio
        ↓
 Environment Recipe / Asset
        ↓
 Hakoniwa Urban Mobility
        ↓
 Car / Drone placement
        ↓
 Scenario
        ↓
 Simulation
```

Environment Studio を使わなくても、同じ schema に従った Recipe を手書き・AI 生成して利用できる構造を維持します。
商用価値は独自フォーマットへのロックインではなく、**環境を速く・簡単に・拡張可能な形で作る UX** に置きます。

## Quick start

```bash
python -m pip install -r requirements.txt
python tools/envstudio.py types                                            # 使える部品の型
python tools/envstudio.py validate recipes/examples/drone-practice-field.yaml   # スキーマ＋MuJoCo の物理検証
python tools/envstudio.py --json resolve recipes/examples/hills-field.yaml # 解決済みの環境（JSON）
python tools/envstudio.py generate recipes/examples/car-test-course.yaml --out-dir build/car-test-course
python -m pytest tests
python tools/env_studio.py start                                          # ブラウザの Studio（http://127.0.0.1:8097/）
python tools/env_studio.py stop
```

Studio（#4 / #5）：左で環境の大きさ・地面（平らな地面／丘の hfield とそのパラメータ）を決め、Catalog の部品をクリックで追加して上面図でドラッグ・回転・複製します（グリッド、近くの部品や端への吸い付き、複数選択、Undo / Redo、コピー＆ペースト）。右の欄は品目のパラメータ定義から自動で作られ、範囲外の値は入りません。3D は生成器の GLB そのもので、全体・車目線（南の端から 1.2 m）・ドローン目線に切り替えられます。編集が止まると MuJoCo で検証し、重なり・はみ出し・地面へのめり込みを上面図に赤く出します（#6）。保存先は `work/recipes/`（例の環境は保存するとコピーになります）。

地図から（#10）：Studio の「地図から」で Leaflet の地図を開き、枠（東西 × 南北 m、地図の中心）を合わせて取り込むと、その範囲の建物と道路の Recipe ができます。

- **取得元**：OpenStreetMap（Overpass API。接続先は `HAKONIWA_OVERPASS_URL`、地図タイルは `HAKONIWA_MAP_TILES` で変更可）か GeoJSON ファイル。
- **座標**：緯度経度は環境の中心を原点とするローカルの m（ENU）に変換します。
- **建物**：外形を押し出した `building-footprint`。高さは height タグ、無ければ階数 × 3 m、それも無ければ種類ごとの既定値。屋根だけの構造（building=roof）は宙に浮いた板にします。
- **道路**：中心線に沿った `road-path`。幅と車線数は、無ければ道路の種類ごとの既定値で補います。
- **出典の記録**：出典（OSM の要素と主なタグ、原点・範囲・投影・© OpenStreetMap contributors / ODbL）は Recipe の `geo` と各物体の `source` に残ります。取得したデータは `work/map-data/<id>.json` に保存されます。
- **取り込み後**：普通の部品として編集・検証・生成できます。

コマンドでも取り込めます：

```bash
python tools/env_map.py --bbox 35.6795,139.7650,35.6822,139.7683 --overpass --out work/recipes/tokyo-station.yaml
```

- データの約束事（Type / Catalog / Recipe、座標、地形、診断）：[docs/data-contract.md](docs/data-contract.md)
- 地図からの変換仕様（取得・座標変換・高さや幅の補完表・出典）：[docs/map-import.md](docs/map-import.md)
- AI エージェント向けの契約（contract / inspect / validate → repair のループ）：[docs/ai-contract.md](docs/ai-contract.md)
- 部品の型：`types/environment-types.yaml`、最初の Catalog：`catalogs/starter/catalog.yaml`
- 例の環境：`recipes/examples/`（ドローン練習場 20 m × 30 m、車のテストコース、丘のフィールド）
- 生成物（#3）：`environment.glb`（Three.js、glTF の x = 東・y = 上・z = -北）、`environment.xml`（MuJoCo、ENU。地形は hfield か z = 0 が上面の板、物体は `object:<id>` の body と形状ごとの `geom:<id>/<形状名>`、線や灯火のような見た目だけの形状は contype 0 の group 2）、`environment.json`（対応表、範囲、地形、sha256、指紋）。同じ Recipe からは同じファイルができます。

座標は ENU（x = 東、y = 北、z = 上）、単位は m と度、原点は環境の中心です（hakoniwa-urban-mobility の世界と同じ）。

## MVP roadmap

MVP は [Issue #1](https://github.com/hakoniwalab/hakoniwa-environment-studio/issues/1) で管理します。

- #2 Environment Type / Catalog / Recipe schema
- #3 GLB / MuJoCo World generator
- #4 Browser 2D Environment Compose editor
- #5 Three.js 3D preview
- #6 MuJoCo validation and machine-readable diagnostics
- #7 Starter Catalog for Car / Drone test environments
- #8 Environment Asset export and Urban Mobility integration
- #9 AI authoring contract for generate / validate / repair loops

## Non-goals for MVP

- 3D CAD の代替
- 任意 mesh の本格的なモデリング
- PLATEAU / City World の統合
- Car / Drone controller の実装
- 自動運転・交通制御ロジック
- 最初から Booth Studio と共通 framework を抽出すること

まず Environment Studio として一度縦に成立させ、Booth Studio との共通部分とドメイン差分が明確になった段階で共通化を判断します。
