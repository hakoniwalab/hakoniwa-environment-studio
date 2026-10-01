# Hakoniwa Environment Studio

Hakoniwa Environment Studio は、Car / Drone / Robot などを動かすための **シミュレーション環境をブラウザで設計するオーサリングツール**です。

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
価値は独自フォーマットへのロックインではなく、**環境を速く・簡単に・拡張可能な形で作る UX** に置きます。

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
python ../hakoniwa-environment-studio/tools/env_studio.py start     # ブラウザの Studio（http://127.0.0.1:28097/）
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

### 書き出し先と hakoniwa-urban-mobility

Environment Studio は、ほかのツールを呼びません。作った環境（Recipe）や PLATEAU の City World を、起動時に渡された**書き出し先**のフォルダへ City World ジョブ（hakoniwa-urban-mobility の World の形式、`schemas/city-world-job.yaml`）として書き出すだけです。

```bash
python ../hakoniwa-environment-studio/tools/env_studio.py start --export-dir <書き出し先のフォルダ>
```

- **hakoniwa-urban-mobility と使うとき：** Urban Studio の City タブの「Environment Studio で作る」が、urban の受け取りフォルダを書き出し先にして Env Studio を起動し、書き出されたものを World として登録します。作ってから車やドローンで走らせるまでの手順は、[hakoniwa-urban-mobility の Quick start](https://github.com/hakoniwalab/hakoniwa-urban-mobility) にあります。
- **書き出すもの：** Studio の画面の「書き出す」（保存した Recipe）、地図ページで生成した City World（できあがると自動で書き出し。「できたもの」の「書き出す」でも）と、「できたもの」に並ぶ Business Pack などの City World。「書き出しを消す」で書き出し先から消せます（元の環境や City World は残ります）。詳しくは [docs/urban-export.md](docs/urban-export.md)。
- **書き出し先なしで起動したとき：** 環境を作る・編集する・検証する・GLB / MJCF を生成する単体のツールとして使えます（書き出しのボタンは出ません）。

Studio（#4 / #5）：左で環境の大きさ・地面（平らな地面／丘の hfield とそのパラメータ）を決め、Catalog の部品をクリックで追加して上面図でドラッグ・回転・複製します（グリッド、近くの部品や端への吸い付き、複数選択、Undo / Redo、コピー＆ペースト）。右の欄は品目のパラメータ定義から自動で作られ、範囲外の値は入りません。3D は生成器の GLB そのもので、全体・車目線（南の端から 1.2 m）・ドローン目線に切り替えられます。編集が止まると MuJoCo で検証し、重なり・はみ出し・地面へのめり込みを上面図に赤く出します（#6）。保存先は `<ws>/recipes/`（例の環境は保存するとコピーになります）。一覧の × で環境を削除できます。すぐには消さず、見た目の GLB（`<ID>.assets/`）と地図から取り込んだ元データ（`<ws>/map-data/<ID>/`）と一緒に `<ws>/trash/<日時>-<ID>/` へ移します（戻すときはそこから `<ws>/` へ戻し、要らなければ手で消します）。例の環境は消せません。別の ID で保存すると、見た目の GLB もその ID の `<ID>.assets/` にコピーするので、元の環境を消してもコピーは壊れません（手で書いた Recipe がほかの環境の `.assets/` を指している場合は、削除を止めてその環境を示します）。部品を動かしたときは、3D の部品をその場で動かし、高さ（地形・道路の上）だけをサーバに聞きます（`POST /api/poses`）。GLB を作り直すのは、形・品目・地面・大きさが変わったときだけです。

地図から（#10）：Studio の「地図から」で Leaflet の地図を開き、範囲を指定して取り込むと、その範囲の建物と道路が部品になった Recipe ができます。範囲の指定は PLATEAU の City World ブラウザと同じです（中心マーカー、区画のドラッグ、四隅のハンドル、half extent 10〜1000 m）。

地図ページの左上のトグルで、**PLATEAU** と **OpenStreetMap** を切り替えます。範囲はそのまま残るので、作業の途中で行き来できます。取れる範囲と精度が違うためです。

| | PLATEAU | OpenStreetMap |
|---|---|---|
| 範囲 | PLATEAU が整備された都市だけ | 世界中どこでも |
| 建物 | LOD2 の形とテクスチャ、建物ごとの当たり判定（P0〜P3） | 外形を押し出した箱（高さは推定を含む） |
| 地形・道路 | 地形（DEM）、道路面、路面標示 | 平らな地面、道路の線から作った面 |

- **PLATEAU：** Business Pack の City World Web UI と同じ画面です。生成条件（建物の当たり判定の細かさ＝Building Physics Level、地形（DEM）が無い所の扱い、当たり判定の減らし方）を選び、「2. データを診断」で範囲に PLATEAU のどのデータがあるか（建物・地形・道路・路面標示・橋の有無と最大 LOD、自治体、診断した 3 次メッシュを地図に表示）を公式のカタログに問い合わせます（ダウンロードはしません）。「3. City Worldを生成」で hakoniwa-envsim に作らせ、進捗（段階と %）を表示し、キャンセルもできます。できあがると「できたもの」タブに移ります。選ぶと範囲を地図にオレンジの破線で示し、当たり判定の内訳を表示します。「3D で見る」で見た目と当たり判定を重ねて見られ、ZIP の取得と削除もできます。Studio では、書き出し先への書き出しと部品としての取り込みもできます。データがなければ「この範囲を OpenStreetMap で作る」で切り替えられます。DEM がまったく無い範囲は、「地形（DEM）が無い所」を「標高 0 m で埋める」にすれば平らな地面で作れます。
- **OpenStreetMap：** 範囲の建物と道路から、PLATEAU と同じ仕組みで City World を生成します（`<ws>/city-worlds/<ID>/`。地面は標高0 mの平面。ID は中心から自動で付き、書き換えられます）。できたものは PLATEAU の City World と同じ「できたもの」に並び、3D・書き出し・部品として取り込む、が同じようにできます。City World には車道が要るので、車道のない範囲（歩道だけなど）は作れません。「この範囲が PLATEAU にあるか確かめる」で、PLATEAU に切り替えて同じ範囲を診断します。
- **部品として取り込む：** 「できたもの」の「部品として取り込む」で、City World を Studio で編集できる部品にし、そのまま Studio の画面に移ります。

- **経路**：地図データは、街データの標準の中間表現である CityGML を経由します。
  1. hakoniwa-envsim の `osm2citygml.py` が、OpenStreetMap（Overpass API）か GeoJSON を CityGML LOD1 にします（変換規則は envsim の `docs/osm-to-citygml.md`）。
  2. hakoniwa-envsim がその CityGML から City World を作ります（`source.kind: files`）。
  3. 部品にするときは、このリポジトリの部品変換ツール `tools/env_citygml.py` が City World の建物・道路を部品にします。
- **部品の単位**：建物 1 棟（CityGML の `bldg:Building`）＝ `building-footprint` の部品 1 つ。道路の面は `road-area` です。建物は切らずに丸ごと使い、はみ出す建物があれば環境のほうを広げます。
- **Business Pack などで作った City World**：hakoniwa-envsim で変換済みの街（ビジネスパックの City World ジョブ：静岡・札幌など）も地図ページの「できたもの」に並び（「ほかのフォルダも探す」で別の場所も探せます）、そのまま部品にできます。envsim が抽出した建物を使うので、City World と同じ建物が同じ位置に並びます。1 棟ずつ動かす・消す・複製することもできます。City World に地形（PLATEAU の DEM）があれば、それが地面になり、建物は移動先の地面の高さに合わせて立ちます。LOD2 のある建物は、PLATEAU のテクスチャ付きの見た目（建物ごとの GLB）で表示され、部品と一緒に動きます。envsim が作った地形（hfield と GLB）・建物ごとの当たり判定（P0〜P3）・道路網などの層は作り直さずにそのまま使うので、**取り込んで編集せずに出力すれば envsim の出力と同じ世界になります**（`tools/env_roundtrip.py` で照合。動かした部品は、その分だけ移して使います）。地図ページで生成した City World（`<ws>/city-worlds/<ID>/`、ダウンロードした CityGML は共有キャッシュで使い回し）は、書き出し先を渡して起動したときはできあがると書き出し先に書き出します。「できたもの」の一覧からも「書き出す」「書き出しを消す」ができます（書き出し先を渡して起動したとき）。
- **建物をマイカタログに登録**：街の建物を選んで「マイカタログに登録」を押すと、その建物（PLATEAU なら LOD2 の見た目、当たり判定 P0〜P3 も）が、ほかの環境にも置ける部品になります。見た目と当たり判定は建物自身の座標（一番低いところが高さ 0）に直して `<ws>/catalogs/my/` にコピーするので、元の街を消しても使えます。項目には元の建物の ID と出典（PLATEAU・OpenStreetMap）が残ります。マイカタログはスターターの部品を全部含むので、環境の Catalog を「マイカタログ」に切り替えれば（使っている部品がすべてある Catalog には、部品があっても切り替えられます）、試験場などに何棟でも置けます（`tools/env_catalog_items.py`）。
- **出典の記録**：部品は gml:id・元のファイル・OSM のタグを `source` に、Recipe は原点・範囲・出典（© OpenStreetMap contributors / ODbL、PLATEAU）を `geo` に持ちます。取得した地図データと CityGML は City World のフォルダ（`<ws>/city-worlds/<ID>/osm/`）に残ります。
- **準備**：hakoniwa-envsim を使います。Quick start の `recipe.py configure` が、無ければ隣（`../hakoniwa-envsim`）に clone します（`HAKONIWA_ENVSIM_ROOT` で別の場所を指せます）。

コマンドでも変換できます（`(hako)` シェルの `hakoniwa-business-pack` で）：

```bash
python ../hakoniwa-envsim/src/city_pipeline/osm2citygml.py --bbox 35.6795,139.7650,35.6822,139.7683 --overpass --out-dir work/recipes/environment-studio/map-data/tokyo
python ../hakoniwa-environment-studio/tools/env_citygml.py --citygml work/recipes/environment-studio/map-data/tokyo --center 35.68085,139.76665 --half-extent 149.787,149.367 --out work/recipes/environment-studio/recipes/tokyo.yaml
```

- データの約束事（Type / Catalog / Recipe、座標、地形、診断）：[docs/data-contract.md](docs/data-contract.md)
- CityGML から部品への変換仕様：[docs/citygml-parts.md](docs/citygml-parts.md)
- City World ジョブの書き出し（書き出し先、「書き出す」ボタン、`tools/env_urban.py`）：[docs/urban-export.md](docs/urban-export.md)
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

## ライセンス

このリポジトリのソフトウェア、Type・Catalog・Recipe、およびドキュメントは [MIT License](LICENSE) で提供します。

次のものは、それぞれの条件に従います。このリポジトリの MIT License は、これらの権利を与えるものではありません。

- ブラウザが読み込むライブラリ：[three.js](https://github.com/mrdoob/three.js)（MIT）、[Leaflet](https://github.com/Leaflet/Leaflet)（BSD-2-Clause）。リポジトリには含めず、実行時に unpkg から読み込みます。
- 依存するリポジトリとパッケージ：[hakoniwa-envsim](https://github.com/hakoniwalab/hakoniwa-envsim)、hakoniwa-urban-mobility、MuJoCo、shapely などの Python パッケージ（`recipes/requirements/environment-studio.txt`）。
- 地図から取り込んだデータ：OpenStreetMap のデータ（© OpenStreetMap contributors、ODbL）、地図タイル、PLATEAU のデータとテクスチャ。取り込んだデータは作業場所（`<ws>/map-data/`、`<ws>/recipes/`）に置かれ、Recipe の `geo` に出典が記録されます。利用・再配布するときは、それぞれの提供元の条件に従ってください。
