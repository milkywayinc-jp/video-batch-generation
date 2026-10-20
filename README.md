# 動画一括生成

Googleスプレッドシートの★付き台本を読み、取得済み音声の調整・行分割・秒数記録・正方形動画の合成を行うCodexスキルです。初期実装として公開しています。

**このリポジトリを閲覧・ダウンロードしただけでは何も実行されません。** スケジューラー、GitHub Actions、インストール時の実行処理、サーバー、Webhook、既存スキルを書き換える処理はありません。Google Sheetsは明示的な読取コマンドで接続し、書込はさらに `--apply --edit-window-confirmed` を指定した場合だけです。利用していない人のPC・広告アカウント・シートには作用しません。

## 現在できること

- ★付きシートの探索、結合ヘッダーの解釈、可変行数のカット抽出。LENや全編注釈を最終カットと誤認しない。
- 140字以下の音声バッチ計画。取得済み音声の1.1倍速化、長い無音の短縮、確認済みのサンプル境界による行分割。
- 素材音声を使うカットの映像・音声の連動編集。無音カットは明示尺を要求。
- 各行Hと、最終カット直後のG/Hへの数値書戻し。直前・直後の再読、所有journal、ローカルロック、更新予定のプレビュー。
- 1080×1080・30fps・H.264/AAC、全面クロップ、字幕の横幅圧縮、2種類の注釈、末尾60フレームの無音、AAC後の音量・尺・フレーム数の計測。

## まだ自動化していないこと

- 音声メーカーへの自動送信・生成。指定サービスで利用可能な接続経路から音声を取得して取り込みます。公開・非公開APIを推測して呼びません。
- 日本語の発話境界の全自動認定。無音点は候補です。実際に試聴してサンプル位置を確認します。
- 自然文で書かれた全編集指定の機械解釈。Codexが共通・個別設定を読み、対応するmanifest値へ反映します。対応できない指定は保留にします。
- 素材の意味理解、被写体との重なりの自動保証。動きの大きい区間は数値で候補選択しますが、開始点・字幕位置は目視確認が必要です。
- 広告入稿、他チャットへの無条件連絡、共同編集者との完全排他。

本番素材・音声が未入力の台本は、計画と不足一覧まで作成します。工程試験の動画を本番台本の完成品として扱いません。1行の読み上げ本文が140字を超える場合は、現在のCLIは内部分割を自動作成せず明示的に停止します。

## 必要な環境

- macOSまたはLinux、Python 3.9以降、ffmpeg/ffprobe（libx264・loudnorm搭載）
- Pillow 10〜12
- 実シートの操作にGoogle Workspace CLI（`gws`）と、利用者自身のSheets認証
- Noto Sans JPの可変フォント（Weight軸400〜900）。フォントファイルは同梱しません。利用者が公式配布元のライセンスを確認し、継続して使える場所へ保存してください。

[gws公式リポジトリ](https://github.com/googleworkspace/cli) / [Noto Sans JP公式配布](https://github.com/google/fonts/tree/main/ofl/notosansjp)

Pythonの依存は、自分の仮想環境に明示的に入れます。既存の共通環境は変更しません。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

以下の `python3` は必要に応じて `.venv/bin/python` に置き換えます。認証トークンを引数・ログ・リポジトリへ保存しないでください。このコードは既存のgws認証を利用し、鍵やブラウザCookieを読み出しません。

## Codexへの導入（任意・手動）

CLIはこのフォルダからそのまま使えます。スキルとして検出させたい人だけ、自分のプロジェクトの `.agents/skills/video-batch-generation/` へ次をコピーしてください。既存の同名フォルダを上書きしないでください。

```text
video-batch-generation/
  SKILL.md
  README.md
  requirements.txt
  agents/
  references/
  scripts/
```

Gitの管理情報、runs、local、認証情報はコピー不要です。共通のAGENTS.mdやグローバルな既存スキルの編集は必要ありません。表示名は「動画一括生成」、呼出名は `$video-batch-generation` です。

## 使い方

すべての出力先は新しいフォルダを指定します。既存の出力は上書きしません。実台本・個人パスを含む出力は `runs/` 配下に置き、公開しないでください。

### 1. 読取と計画（シート変更なし）

```bash
python3 scripts/video_batch.py inspect \
  --spreadsheet-id YOUR_SPREADSHEET_ID --out runs/job-001/inspection
```

sheetIdごとの `.plan.json` と `.edit.json`、元のsnapshotができます。シート名・件数・最終行は固定しません。★付き全対象を毎回調べます。共通設定、個別指定、既知の素材保留を読み、editを埋めます。入力列はA:Kの台本書式です。詳細は [manifestの仕様](references/manifest.md) を参照してください。

### 2. 取得した通常ナレーションの調整

```bash
python3 scripts/video_batch.py adjust-audio \
  --plan runs/job-001/inspection/SHEET_ID.plan.json \
  --batch-id batch-001 --input /absolute/path/acquired-audio.mp4 \
  --out runs/job-001/batch-001
```

`adjusted.wav` と `adjusted.audio.json` ができます。通常生成だけを1.1倍速にします。元動画音声の指定行はこのコマンドへ送らず、連動区間をeditへ記録します。

調整後の音声を試聴し、各バッチの `path`、元行数+1個の `boundaries`、確認済みの `verified` を設定してください。単位は48kHzのサンプル番号で、先頭0、末尾はファイル全サンプル数です。文字数比例で境界を捏造しないでください。

### 3. 尺の確定と素材検査（シート変更なし）

```bash
python3 scripts/video_batch.py prepare \
  --plan runs/job-001/inspection/SHEET_ID.plan.json \
  --edit runs/job-001/inspection/SHEET_ID.edit.json \
  --out runs/job-001/prepared
```

各行の尺はフレーム単位で確定し、最後だけ無音60フレームを含めます。素材不足・未確定区間は停止し、勝手にループしません。`prepared.json` が尺の記録です。

### 4. 書戻し予定の確認と実更新

まず次のコマンドだけで更新予定のJSONを確認します。

```bash
python3 scripts/video_batch.py writeback \
  --plan runs/job-001/inspection/SHEET_ID.plan.json \
  --prepared runs/job-001/prepared/prepared.json --state-dir local/sheet-state
```

実更新する場合だけ、同じコマンドへ `--apply --edit-window-confirmed` を追加します。`--edit-window-confirmed` は共同編集者がその短い更新時間に編集しないことを確認した意味です。確認なしにCodexが付けないでください。Sheets APIのセル更新と人手編集は完全排他にできず、直前・直後の照合でも上書きを完全には防げません。

同じシートの全runで同じ `state-dir` を使います。同一ユーザー・同一ホストの実行は、state-dirが異なってもシートID基準の一時ロックで競合を防ぎます。別端末は排他できません。journalは所有権と障害復旧の記録なので保持してください。前回の合計が未所有なら、自動で削除・新設しません。通信結果不明の書込を盲目的に再送しません。

### 5. レンダリングと確認

本番は書戻し結果のreceiptを指定します。

```bash
python3 scripts/video_batch.py render \
  --plan runs/job-001/inspection/SHEET_ID.plan.json \
  --prepared runs/job-001/prepared/prepared.json \
  --receipt local/sheet-state/RECEIPT_DIRECTORY/journal.json \
  --out runs/job-001/rendered
```

シートを更新しない工程試験では、`--receipt ...` の代わりに `--local-only` を明示します。シート名のMP4、カット別字幕PNG、`validation.json` が出ます。出力は機械検証の合格までで、`production_complete` はfalseです。目視・試聴と最新シート照合を経て制作担当が完成判断してください。最後の連結やレンダリングで無音2秒を再追加しないでください。

音量の機械判定はAAC後で−7.2±0.3 LUFS、true peakは−1 dBTP以下です。最後の60フレームを無音とし、AACの残響確認には先頭1フレームの許容を設けます。無音だけの台本にはLUFSの有限値を要求しません。量子化・音量測定結果はJSONに残します。

この音量とピークを同時に満たすため、必要な場合はコンプレッサーと先読みリミッターを適用し、その試行と測定値を記録します。音の自然さは必ず試聴してください。BGMは一度だけデコードして連続位置から切り出し、正規化前のミックスでクリップする音量指定はエラーにします。

## 検証状況とデータの公開範囲

合成データの境界テストに加え、開発環境では実Google Sheetsの読取と、ローカル実動画で編集工程を検証します。実シートの書込・音声メーカー生成・本番全編制作は未検証のため、完了したとは扱いません。確認内容の現状は [検証記録](references/validation.md) を参照してください。

```bash
python3 -m unittest discover -s tests -v
python3 scripts/video_batch.py --help
```

公開対象はコード・スキル・説明・合成テストだけです。実台本、シートID、入稿先、広告素材、フォント、音声、トークン、社内パスは含めません。`.gitignore` は予防策なので、公開前にはGitの実際の追跡ファイルも点検してください。GitHub上での公開は、音声サービスやフォントなど第三者の利用条件を変更するものではありません。
