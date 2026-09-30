# Workspace Recipes（非公開）

このディレクトリの `recipes/*.yaml` は、hakoniwa-business-pack の Recipe 形式で書いた、このリポジトリ専用のレシピです。
ビジネスパックの公開のカタログやレシピには載せません。ビジネスパックの道具だけを借りて使います。

| レシピ | 内容 |
|---|---|
| `citygml-parts.yaml` | CityGML（PLATEAU、OpenStreetMap から変換したもの）を Studio の部品にする。hakoniwa-envsim を依存として取り込む |

ビジネスパックの道具で、依存の確認と取得をします：

```bash
python ../hakoniwa-business-pack/tools/recipe.py plan --recipe hakoniwa/recipes/citygml-parts.yaml
python ../hakoniwa-business-pack/tools/recipe.py doctor --recipe hakoniwa/recipes/citygml-parts.yaml
```

- `recipe_local_requirements` に書いた hakoniwa-envsim は、このリポジトリの隣（`../hakoniwa-envsim`）に置かれます。`HAKONIWA_ENVSIM_ROOT` で別の場所を指せます。
- 出力をこのリポジトリの中に置くには、`HAKONIWA_WORK_DIR=$PWD/work` を付けて実行します。

Environment Studio の Recipe（`recipes/`、環境そのもの）とは別物です。
