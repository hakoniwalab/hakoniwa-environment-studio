# hakoniwa-urban-mobility への書き出し（#8）

Environment Studio で作った環境を、hakoniwa-urban-mobility の World として使えるようにする仕組みです。実装は `tools/env_urban.py` です。

作ってから urban で車を走らせるまでの手順は、README の「作った環境で車を走らせる（通しの手順）」にあります。

## 1. 受け渡しの決まり

urban-mobility は World を **City World ジョブ**（フォルダ）で受け取り、自分で City Asset として登録します（`tools/urban_assets.py register-city`）。ジョブの構成とレシートの中身の決まりは、urban-mobility 側の `schemas/city-world-job.yaml` にあり（`docs/asset-contract.md` 6.3 節）、`tools/city_world_job.py check` で調べられます。Studio はこの決まりに合わせて書き出します。

## 2. 書き出すもの

Studio の Recipe workspace の `urban/<Recipe ID>/`（`$HAKONIWA_WORK_DIR/recipes/environment-studio/urban/<Recipe ID>/`）に、次の構成で書きます（ジョブのフォルダ名が City Asset の ID になります）。

| ファイル | 中身 |
|---|---|
| `build/world/city-world-receipt.json` | レシート：座標系、原点、範囲、ファイル、`producer`（Recipe と指紋） |
| `build/world/city-world.xml` | World の MJCF（urban の座標系 x 北・y 西・z 上） |
| `build/world/city-world.glb` | Studio の GLB そのもの（x 東・y 上・z 南向きが負） |
| `build/components/terrain/` | 地形の格子（`terrain.hf`）、`terrain.xml`、`terrain-receipt.json` |
| `viewer/` | `city-world.glb` の写しと、当たり判定の表示用 GLB（`tools/env_colliders.py`）とそのレシート |

- **MJCF**：Studio が生成する世界を、urban の座標系に回したものです。
  - 地面は最上位の hfield です。envsim の地形は envsim の格子ファイルそのまま、丘は Studio の格子、平らな地面は 2×2 の平らな格子（urban の平らな World と同じ）です。
  - それ以外の部品は、z 軸まわりに −90° 回した 1 つの body の中に置きます。
  - 最上位は `asset` と `worldbody` だけです。回転はすべて四元数にしているので、合成される相手のコンパイラの角度設定に左右されません。
  - 当たり判定のない見た目だけの形（白線など）は入れません。見た目は GLB が受け持ちます。
  - 照明も入れません。urban は車のモデルの照明（`sun`・`fill_light`）を残したまま World を合成するので、同じ名前があると合成が止まります。
- **地形の格子**：envsim のファイルと同じ並び（行は西向き、列は北向き）です。Studio の MJCF の丘の格子も同じ並びにしているので、格子のマスを分ける対角線まで同じになり、Studio で検証した地面と urban の地面は一致します。
- **種類**：地図の原点がある Recipe（地図や City World から取り込んだもの）は `kind: city`、ないものは `kind: plain`（原点 0, 0。urban は Map Viewer の代わりに Three.js を開きます）です。
- **指紋**：レシートの `producer.fingerprint` は生成物の指紋（`env_generate.fingerprint`）です。環境を変えて書き出し直すと変わり、登録し直すと urban 側の版も変わります。

## 3. 使い方

- **書き出し先：** Studio は urban を呼びません。`env_studio.py start --export-dir <フォルダ>` で渡されたフォルダに、City World ジョブを `<ID>/` として書き出すだけです。ジョブは隣で書き終えてから一度に移すので、書き出し先を見ているツールが書きかけを拾うことはありません。各ジョブには `job.json`（名前 `title`、元になった Recipe か City World `source`）が付きます。urban-mobility は、Urban Studio の「Environment Studio で作る」でこの書き出し先を自分の受け取りフォルダにして Studio を起動し、現れたジョブを自分で検査して City として登録し、消えたジョブの登録を外します。
- **Studio の画面：** 環境を保存してから、上の「書き出す」を押します（書き出し先を渡して起動したときだけ出ます）。
- **PLATEAU の City World をそのまま書き出す：** 地図ページのワークスペースの街の「書き出す」、または地図ページの PLATEAU で生成した City World（書き出し先を渡して起動したときは、できあがると自動で書き出します。生成結果の「書き出す」「書き出しを消す」でも切り替えられます）。envsim の City World はもともとこの形式なので、変換しません。書き出すのは小さなジョブ（receipt、建物の外形 `build/city-world-lod1.json`、`viewer/`）で、receipt は City World の MJCF や GLB を絶対パスのまま指します。City World を消すと、書き出したジョブも使えなくなります。「書き出しを消す」で書き出し先から消せます（City World 自体は残ります）。
- **コマンド：** `tools/env_urban.py export <Recipe> [--out DIR]`（Recipe をジョブとして書き出す。登録はしません）

## 4. 確かめたこと

- urban のチェックに合格：平らな試験場（`car-test-course`・`drone-practice-field`）、丘（`hills-field`）、envsim の原本を使う街（`sapporo-351-exact`・`numazu-exact`）。
- **形**：書き出した MJCF の当たり判定の形は、すべて Studio の世界と位置・向きが一致します（10⁻¹⁴ m の桁）。原本を使う街は、envsim の `city-world.xml` とそのまま一致します（地形の格子データも完全一致）。
- **初期高さ**：urban の初期高さの計算（`tools/world_height.py`）が返す地面・屋根・障害物の上の高さは、Studio の世界と一致します（400 点ずつ、差は最大 7×10⁻⁸ m）。
- **登録と計画**：`register-city` で登録でき、urban のゴルフカート 1 台の Composition の `plan` が通ります。
- **configure**：`car-test-course` とゴルフカート 1 台の Composition で、urban の `configure` が通りました。依存の clone（mbody-registry など 5 つ）、車の物理プラントのビルド、World と車の合成、MuJoCo 3.13 での MJB のコンパイルと読み直し、初期位置の高さ（z = 0.47 m）まで確認しています。
