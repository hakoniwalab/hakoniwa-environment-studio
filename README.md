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

Environment Studio は、[箱庭ビジネスパック](https://github.com/hakoniwalab/hakoniwa-business-pack)の Workspace で使います。Python は Business Pack の Foundation Python（3.12）に一本化し、個別の venv は作りません。依存（hakoniwa-envsim・hakoniwa-urban-mobility と Python パッケージ）は、このリポジトリの Business Pack Recipe [recipes/business-pack/environment-studio.yaml](recipes/business-pack/environment-studio.yaml) に書いてあり、`recipe.py configure` が用意します。

`hakoniwa-business-pack` で Workspace に入ります（プロンプトの先頭に `(hako)` が付きます）：

```bash
python3.12 tools/workspace.py enter
```

以降は `(hako)` シェルの `hakoniwa-business-pack` で実行します。

```bash
python tools/recipe.py configure --recipe ../hakoniwa-environment-studio/recipes/business-pack/environment-studio.yaml
python tools/recipe.py doctor    --recipe ../hakoniwa-environment-studio/recipes/business-pack/environment-studio.yaml
python ../hakoniwa-environment-studio/tools/env_studio.py start     # ブラウザの Studio（http://127.0.0.1:8097/）
python ../hakoniwa-environment-studio/tools/env_studio.py status
python ../hakoniwa-environment-studio/tools/env_studio.py open      # 動いている Studio をブラウザで開く
python ../hakoniwa-environment-studio/tools/env_studio.py stop      # Workspace を抜ける（exit）前に止める
```

コマンドの道具とテスト：

```bash
python ../hakoniwa-environment-studio/tools/envstudio.py types                                        # 使える部品の型
python ../hakoniwa-environment-studio/tools/envstudio.py validate ../hakoniwa-environment-studio/recipes/examples/drone-practice-field.yaml
python -m unittest discover -s ../hakoniwa-environment-studio/tests
```

Studio の作業データは、Business Pack の Recipe workspace `work/recipes/environment-studio/`（`$HAKONIWA_WORK_DIR/recipes/environment-studio`、以下 `<ws>`）に置きます。Workspace の外では Studio は起動せず、入り方を表示して止まります。

### 作った環境で車を走らせる（通しの手順）

Environment Studio で作った環境を hakoniwa-urban-mobility に登録し、車と組み合わせてシミュレーションするまでの流れです。どれも `(hako)` シェルの `hakoniwa-business-pack` で実行します。

1. **Studio を開く**

   ```bash
   python ../hakoniwa-environment-studio/tools/env_studio.py start --open-browser
   ```

   動いている Studio をあとで開き直すときは `env_studio.py open` です。

2. **環境を作る**：部品を置くか、「地図から」で街を取り込み、保存します。
3. **urban に登録する**：上の「urban-mobility へ」を押します。書き出し（`<ws>/urban/<ID>/`）、urban のチェック、City Asset への登録までを行い、登録した ID が表示されます（詳しくは [docs/urban-export.md](docs/urban-export.md)）。
4. **Urban Studio を開く**

   ```bash
   python ../hakoniwa-urban-mobility/tools/urban_studio.py start --open-browser
   ```

   開き直すときは `urban_studio.py open`、止めるときは `urban_studio.py stop` です。
5. **車と組み合わせる**：Urban Studio の Compose で、World に 3 で登録した ID を選び、車（ゴルフカート、操作は `rc`）を追加して道路の上に置き、保存します。地図の原点がある環境は地図、原点のない環境は 3D ビューで置きます。
6. **シミュレーションする**：Simulation でその Composition を選び、`configure` のあと `start` を押します。Viewer が開いたらコントローラで走らせ、終わったら `stop` を押します。
7. **片付け**：Workspace を抜ける（`exit`）前に、両方の Studio を止めます。

   ```bash
   python ../hakoniwa-urban-mobility/tools/urban_studio.py stop
   python ../hakoniwa-environment-studio/tools/env_studio.py stop
   ```

環境を直したときは、Studio で保存して「urban-mobility へ」を押し直せば、同じ ID の City が新しい版で登録し直されます。

Studio（#4 / #5）：左で環境の大きさ・地面（平らな地面／丘の hfield とそのパラメータ）を決め、Catalog の部品をクリックで追加して上面図でドラッグ・回転・複製します（グリッド、近くの部品や端への吸い付き、複数選択、Undo / Redo、コピー＆ペースト）。右の欄は品目のパラメータ定義から自動で作られ、範囲外の値は入りません。3D は生成器の GLB そのもので、全体・車目線（南の端から 1.2 m）・ドローン目線に切り替えられます。編集が止まると MuJoCo で検証し、重なり・はみ出し・地面へのめり込みを上面図に赤く出します（#6）。保存先は `<ws>/recipes/`（例の環境は保存するとコピーになります）。一覧の × で環境を削除できます。すぐには消さず、見た目の GLB（`<ID>.assets/`）と地図から取り込んだ元データ（`<ws>/map-data/<ID>/`）と一緒に `<ws>/trash/<日時>-<ID>/` へ移します（戻すときはそこから `<ws>/` へ戻し、要らなければ手で消します）。例の環境は消せません。別の ID で保存すると、見た目の GLB もその ID の `<ID>.assets/` にコピーするので、元の環境を消してもコピーは壊れません（手で書いた Recipe がほかの環境の `.assets/` を指している場合は、削除を止めてその環境を示します）。部品を動かしたときは、3D の部品をその場で動かし、高さ（地形・道路の上）だけをサーバに聞きます（`POST /api/poses`）。GLB を作り直すのは、形・品目・地面・大きさが変わったときだけです。

地図から（#10）：Studio の「地図から」で Leaflet の地図を開き、範囲を指定して取り込むと、その範囲の建物と道路が部品になった Recipe ができます。範囲の指定は PLATEAU の City World ブラウザと同じです（中心マーカー、区画のドラッグ、四隅のハンドル、half extent 10〜1000 m）。

- **経路**：地図データは、街データの標準の中間表現である CityGML を経由します。
  1. hakoniwa-envsim の `osm2citygml.py` が、OpenStreetMap（Overpass API）か GeoJSON を CityGML LOD1 にします（変換規則は envsim の `docs/osm-to-citygml.md`）。
  2. このリポジトリの部品変換ツール `tools/env_citygml.py` が、それを部品にします。
- **部品の単位**：建物 1 棟（CityGML の `bldg:Building`）＝ `building-footprint` の部品 1 つ。道路の面は `road-area` です。建物は切らずに丸ごと使い、はみ出す建物があれば環境のほうを広げます。
- **ワークスペースの街**：hakoniwa-envsim で変換済みの街（ビジネスパックの City World ジョブ：静岡・札幌など）を地図ページの「ワークスペースの街」で探して、そのまま部品にできます。envsim が抽出した建物を使うので、City World と同じ建物が同じ位置に並びます。1 棟ずつ動かす・消す・複製することもできます。City World に地形（PLATEAU の DEM）があれば、それが地面になり、建物は移動先の地面の高さに合わせて立ちます。LOD2 のある建物は、PLATEAU のテクスチャ付きの見た目（建物ごとの GLB）で表示され、部品と一緒に動きます。envsim が作った地形（hfield と GLB）・建物ごとの当たり判定（P0〜P3）・道路網などの層は作り直さずにそのまま使うので、**取り込んで編集せずに出力すれば envsim の出力と同じ世界になります**（`tools/env_roundtrip.py` で照合。動かした部品は、その分だけ移して使います）。地図ページの「PLATEAU から City World を作る」では、選んだ範囲の City World を hakoniwa-envsim に作らせ（`<ws>/city-worlds/<ID>/`、ダウンロードした CityGML は共有キャッシュで使い回し）、終わったらそのまま部品として取り込みます。
- **出典の記録**：部品は gml:id・元のファイル・OSM のタグを `source` に、Recipe は原点・範囲・出典（© OpenStreetMap contributors / ODbL、PLATEAU）を `geo` に持ちます。取得した地図データと CityGML は `<ws>/map-data/<id>/` に残ります。
- **準備**：hakoniwa-envsim を使います。Quick start の `recipe.py configure` が、無ければ隣（`../hakoniwa-envsim`）に clone します（`HAKONIWA_ENVSIM_ROOT` で別の場所を指せます）。

コマンドでも変換できます（`(hako)` シェルの `hakoniwa-business-pack` で）：

```bash
python ../hakoniwa-envsim/src/city_pipeline/osm2citygml.py --bbox 35.6795,139.7650,35.6822,139.7683 --overpass --out-dir work/recipes/environment-studio/map-data/tokyo
python ../hakoniwa-environment-studio/tools/env_citygml.py --citygml work/recipes/environment-studio/map-data/tokyo --center 35.68085,139.76665 --half-extent 149.787,149.367 --out work/recipes/environment-studio/recipes/tokyo.yaml
```

- データの約束事（Type / Catalog / Recipe、座標、地形、診断）：[docs/data-contract.md](docs/data-contract.md)
- CityGML から部品への変換仕様：[docs/citygml-parts.md](docs/citygml-parts.md)
- hakoniwa-urban-mobility への書き出し（「urban-mobility へ」ボタン、`tools/env_urban.py`）：[docs/urban-export.md](docs/urban-export.md)
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
