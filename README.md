# 動画一括生成

Googleスプレッドシートを**制作ルールの正本**にする、Codex用のスキル・プラグインとローカル編集CLIです。
全体設定・共通設定・行別指定を毎回Codexが読み、音声と動画の処理へ反映します。サイズ・fps・フォント・音声速度・末尾尺などをスキルの固定ルールとして保存しません。

ダウンロード、clone、`--help`、Pythonのimportだけでは、チャット作成・Google Driveへの書込・動画生成は実行されません。常駐処理・自動実行フック・広告入稿はありません。

## 導入時と制作時

1. パッケージをCodexへ導入し、利用者が「セットアップ」を開始します。
2. 保存済みの配布基準版を照合し、台本・設定・数式・結合・書式を含む自分用のGoogleスプレッドシートを作ります。
3. コピーURLと制作の開始文を渡し、利用できる公式機能で制作チャットを準備します。
4. 制作では、その利用者のコピーの最新ルールを読みます。コピーの設定を変えれば、その利用者の制作に反映されます。

**配布基準版は`2026-10-20-v1`に固定**しています。元の作業シートを後から編集しても、配布する形は自動で変わりません。別の形を配布したい場合は、新しい版を明示的に保存します。既存の利用者のコピーも書き換えません。

基準版の台本はGoogle Driveのアクセス制限下に置きます。公開GitHubには台本本文・原本/基準版のファイルID・素材フォルダID・チャット履歴を含めません。利用者は管理者から基準版URLを受け取るか、自分がアクセスできる基準版をDriveで選びます。**GitHubのダウンロードだけで私有シートへのアクセス権は得られません。**

## 必要な環境

- Codexと、このパッケージのスキル/プラグインを利用できる環境。
- Python 3.9以上、`ffmpeg`、`ffprobe`、`requirements.txt`のPillow。
- 認証済みのGoogle Workspace CLI `gws`。Sheetsの読取・必要時の書戻しと、Driveの読取/コピー権限。
- シートが指定するフォントの実ファイル、素材、取得済みナレーション。利用者が使える場所へ保存してください。
- 新規チャットの自動作成には、その環境で利用できる公式のチャット作成機能が必要です。

認証情報は同梱しません。依存関係や認証を勝手にインストール/変更する処理もありません。

## Codexへの導入

このリポジトリ全体を取得し、Codexのプラグイン導入機能でパッケージとして追加してください。入口は`plugin.json`、セットアップは`skills/setup/SKILL.md`、制作は`skills/video-batch-generation/SKILL.md`です。

プラグイン導入機能がない環境では、リポジトリ全体を単体スキルのフォルダとして導入できます。ルートの`SKILL.md`が入口です。`SKILL.md`だけをコピーするとCLIや参照資料が欠けるため、フォルダ全体を保持してください。

導入後の開始文：

> 動画一括生成をセットアップして、新しい制作チャットと自分用の台本シートのコピーを作ってください。

公式onboardingは新しい会話で起動する場合と、導入中の既存会話を使う場合があります。新規作成機能が使えない環境では、コピーURLと開始文を渡して手動で新しいチャットを開きます。ZIPのダウンロード自体をチャット作成の契機にはできません。

仕様：[OpenAI公式・プラグインのonboarding](https://developers.openai.com/plugins/build/plugins)

## セットアップCLI

以下はパッケージルートで実行します。`SOURCE_ID`は管理者が保存した基準版、`PRIVATE_STATE`は利用者の私有状態フォルダです。

```sh
python3 scripts/video_batch.py setup \
  --source-id SOURCE_ID \
  --release templates/releases/2026-10-20-v1.json \
  --state-dir PRIVATE_STATE
```

初期動作は照合と予定の表示のみです。明示的なセットアップ実行時に`--apply`を付けると、本人のMy Driveの非共有`ChatGPT`フォルダへネイティブコピーを作ります。共有ドライブ・他人所有の保存先・共有された同名フォルダは初期対応範囲外です。元シートや基準版の内容・共有設定は変更しません。

コピー前に状態を保存し、Driveにもトランザクション識別子を付けます。通信結果が不明なら検索照合し、結果がゼロでも自動で再送しません。再実行は同じstateを使ってください。別端末や状態消失時は、既存候補を確認してから新規/再利用を決めます。

候補選択後は`--reuse-copy-id ID`、別のコピーを明示的に作る場合は`--create-another`を使います。同じworkspaceに記録済みのコピーは、そのまま再開できます。

素材・保存先・引継ぎ先を確認し、`material`、`output`、`handoff`を持つ私有JSONへ記録します。未確定は`pending`、不要は`none`とします。

```sh
python3 scripts/video_batch.py setup-finish \
  --state-dir PRIVATE_STATE --destinations DESTINATIONS_JSON \
  --chat-id VERIFIED_CHAT_ID
```

手動でチャットを開く場合は`--chat-id`の代わりに`--manual-handoff`。このCLI自体はチャットを作りません。setupスキルが公式ツールと連携します。`production-chat-prompt.txt`とコピーURLが開始用の引継ぎです。

シートのコピーは、リンク先の素材フォルダ・動画・保存先・別のチャットまでは複製しません。旧保存先へ自動で書き込まず、利用者の接続と保存先を確認します。

## シートから動画まで

テンプレートの構造は、全体設定、項目別共通設定、台本ヘッダー、カット行です。対象は★付きタブ。結合や行数は実シートから判定し、合計は実際の最終カット直後へ置きます。空のLEN数式行をカットとして数えません。

```sh
python3 scripts/video_batch.py inspect --spreadsheet-id WORKING_COPY_ID --out runs/inspect
python3 scripts/video_batch.py resolve --plan RAW_PLAN --interpretation INTERPRETATION --out runs/resolved
```

`inspect`は設定の読取まで行います。**Codexが自然文を解釈**し、セル出典を添えたその実行限りのinterpretationを作ります。`resolve`が型・単位・対応値・全セルの解釈・出典を検証し、実行planと音声バッチを作ります。利用者にJSONを恒久ルールとして手入力・保守させる設計ではありません。

優先順位は行別指定→項目別共通設定→全体設定。変更を検出したら古い実行設定や音声を再利用せず、再読して解釈し直します。対応外・曖昧な指定や不足した必須値は、セルを示して保留します。

```sh
python3 scripts/video_batch.py adjust-audio --plan PLAN --batch-id BATCH_ID --input AUDIO --out runs/audio
python3 scripts/video_batch.py prepare --plan PLAN --edit EDIT --out runs/prepared
python3 scripts/video_batch.py writeback --plan PLAN --prepared PREPARED --state-dir PRIVATE_STATE
python3 scripts/video_batch.py render --plan PLAN --prepared PREPARED --out runs/render --local-only
```

音声取得→シートの速度/無音調整→試聴した行境界→実測尺→書戻し→その尺の映像と文字、の順で進めます。最後の無音尺はシートに従い、タイムライン生成時だけ一度追加します。0秒にも対応します。元動画音声は確認した区間で映像と連動させ、通常TTSの速度変更へ送らない構成です。

書戻しは予定表示が初期動作。実書込はユーザーの書込指示と短い編集停止時間を確認後、`--apply --edit-window-confirmed`。G/Hの値のみを対象とし、元の書式・数式・結合・他のデータを変更しません。前回合計は自分のjournalが所有を確認できるものだけ移動します。書込直前と直後に、数式の再計算結果を含めて再読します。Google Sheetsの読取と書込は人の同時編集と原子的ではないため、編集停止時間が必要です。

本番renderは`--local-only`の代わりに`--receipt CONFIRMED_RECEIPT`を使います。機械検証合格は目視・試聴の完了を意味しません。本番完成には両方が必要です。

契約と対応値は[ルール契約](references/rules.md)、実素材の指定は[manifest](references/manifest.md)、実施済みの検証は[validation](references/validation.md)。

## 現段階の対応範囲

- シート由来のサイズ・有理数fps、cover/contain、字幕/注釈のフォント・サイズ・色・縁・位置・改行・幅処理。
- 音声バッチ上限、通常ナレーション速度、無音調整、末尾尺、音量目標・ピーク上限、BGMゲイン。
- H.264/AACとハードカット、尺不足時のエラー。別コーデック・フェード・任意の編集効果は対応外として返します。
- 内部音声編集は48kHzモノラル16-bit PCM。出力音声はAAC。フレーム→サンプルは累積境界から計算し、非整数fpsの丸め誤差をカットごとに積み上げません。

TTSサービスの自動生成API、発話境界の全自動認識、素材未指定の全編制作、広告入稿は未実装です。シートに音声サービスURLが書かれていても、そのサービスへの接続実装や利用許諾まで用意されたことにはなりません。

v0.1の固定値入りplan/edit/preparedとは互換性を持たせていません。最新シートをinspect/resolveし直してください。配布基準版の保存と、制作ルールをスキルへ固定することは別です。

## 検証

```sh
VIDEO_BATCH_TEST_FONT=/absolute/path/to/variable-font.ttf \
  python3 -m unittest discover -s tests -v
```

ffmpegや指定フォントがない場合は該当テストがskipになります。実サービスへ書き込むテストは自動実行しません。私有の`runs/`、`local/`、state、素材、認証情報はGitへ含めないでください。
