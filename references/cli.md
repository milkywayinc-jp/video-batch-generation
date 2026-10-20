# CLIの使い方

以下はパッケージルートで実行します。`SOURCE_ID`は管理者が保存した基準版、`PRIVATE_STATE`は利用者の私有状態フォルダです。

```sh
python3 scripts/video_batch.py setup \
  --source-id SOURCE_ID \
  --release templates/releases/2026-10-20-v2.json \
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

優先順位は行別指定→項目別共通設定→全体設定。変更を検出したら再読して解釈し直します。音声再利用の判断は[制作確認](production-checks.md)に従い、古い実行設定・prepared・receiptは流用しません。対応外・曖昧な指定や不足した必須値は、セルを示して保留します。

```sh
python3 scripts/video_batch.py adjust-audio --plan PLAN --batch-id BATCH_ID --input AUDIO --out runs/audio
python3 scripts/video_batch.py prepare --plan PLAN --edit EDIT --out runs/prepared
python3 scripts/video_batch.py writeback --plan PLAN --prepared PREPARED --state-dir PRIVATE_STATE
python3 scripts/video_batch.py render --plan PLAN --prepared PREPARED --out runs/render --local-only
```

音声取得→シートの速度/無音調整→試聴した行境界→実測尺→書戻し→その尺の映像と文字、の順で進めます。最後の無音尺はシートに従い、タイムライン生成時だけ一度追加します。0秒にも対応します。元動画音声は確認した区間で映像と連動させ、通常TTSの速度変更へ送らない構成です。

書戻しは予定表示が初期動作。実書込はユーザーの書込指示と短い編集停止時間を確認後、`--apply --edit-window-confirmed`。Hの秒数・合計を対象とし（旧Gラベルは自分のjournal所有分だけ削除）、元の書式・数式・結合・他のデータを変更しません。前回合計は自分のjournalが所有を確認できるものだけ移動します。書込直前と直後に、数式の再計算結果を含めて再読します。Google Sheetsの読取と書込は人の同時編集と原子的ではないため、編集停止時間が必要です。

旧receiptでの再開時も、旧Gラベルが残っていれば削除を新しい書込として扱い、予定表示・編集停止・直前直後の読戻しを適用します。元planと現在シートが異なる場合は、最新シートをinspect/resolve/prepareし直します。確認済みjournalをG削除だけの記録で置換せず、Hの所有情報を保持します。

本番renderは`--local-only`の代わりに`--receipt CONFIRMED_RECEIPT`を使います。機械検証合格は目視・試聴の完了を意味しません。本番完成には両方が必要です。

音量合わせは声の自然さを保つ範囲で行います。音量目標とピーク上限の組み合わせによっては、強い歪みを加えずに両立できません。その場合は書き出しを止め、シートの音量目標を見直します。極端に速い圧縮で数値だけを合わせたり、利用者の許可なく目標を下げたりしません。再試行は同じfloat音声から行い、処理を重ねません。秒数を書き戻す前の準備段階で、納品と同じAAC設定へ試し変換し、変換後の音量・ピーク上限も検査します。

契約と対応値は[ルール契約](rules.md)、実素材の指定は[manifest](manifest.md)、実施済みの検証は[validation](validation.md)。
