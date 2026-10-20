# シート由来のルール契約（schema 2）

Codexが自然文を解釈して作る、実行ごとの中間データ。利用者の恒久設定や第二のルール集ではない。制作値の既定値は定義しない。全体設定・共通設定・個別指定の優先を解決し、セルに値がない場合や対応外はunresolvedへ記録する。

## interpretationのトップレベル

| フィールド | 内容 |
| --- | --- |
| values | video / audio / styles / material / naming の共通の解決値 |
| cuts | 台本順のrow / mode / audio / styles / bgm_gain_db |
| provenance | 各値のパス→出典セルの配列。例：values.video.width→[A1] |
| interpretations | plan.rule_inputsの全セル→自然文の解釈。無視したセルを残さない |
| unresolved | 未対応・曖昧な指定。不足があればresolveは停止 |

ファイルを作るのはCodex。`inspect`のinterpretation雛形を埋め、`resolve`へ渡す。元シートのファイルID・タブID・範囲・入力値/式・effective value・schemaと合わせてハッシュが結び付く。

## valuesの型と対応値

| オブジェクト | 必須フィールドと単位 |
| --- | --- |
| video | width/height：偶数px、fps：有理数文字列、codec：h264、audio_codec：aac、fit：cover/contain、background：色文字列 |
| audio | provider：指定のサービス等、batch_limit：文字上限（改行・UTF-16も検証）、speed：倍率、max_pause_seconds：最大無音秒/null（制限なし）、retained_pause_seconds：詰めた後に残す秒、trim_edges：真偽、tail_seconds：末尾無音秒、loudness_lufs：LUFS、peak_dbtp：dBTP、level_match：真偽 |
| styles | caption/note1/note2：下記スタイル。表示する本文が一切ない項目はnull可 |
| material | transition：hard、selection：motion/manual、shortage：error |
| naming | video：sheet/sheet-without-star、audio：シートの音声命名規則の記述 |

各スタイルはfont（ファミリー）、weight、size（整数px）、fill、stroke_fill、stroke（整数px）、spacing（整数px）、align（left/center/right）、x/y（px）、anchor、max_width（整数px）、overflowを持つ。anchorはtop-left/top-center/top-right/center/bottom-left/bottom-center/bottom-right。overflowはcompress-width/wrap/error。compress-widthは高さを保ち、幅だけを圧縮する。

行別cutsのmodeはnarration/source/silence。audioは共通音声の上書きで、provider/batch_limit/speed/max_pause_seconds/retained_pause_seconds/trim_edgesのみ。末尾とマスタリングは動画全体の設定。stylesは項目別スタイルの差分。bgm_gain_dbはシート由来のゲイン、BGMなしはnull。

スタイルの追加効果は任意。glowはcolor/radius（px）/opacity、shadowはcolor/radius/opacity/dx/dy、bandはfill/opacity/pad_x/pad_y/full_width（真偽）。未指定/nullはその効果なし。帯・発光・影も個別指定から出典付きで解決し、共通の「帯なし」を固定ルールにはしない。

各共通値の末端と行別のmode・bgm_gain_db・audio/styles差分の末端すべてにprovenanceが必要。例：`cuts.0.styles.caption.fill`。本文だけでなく各個別指定セルもinterpretationsに含める。範囲内で選んだ制作判断（サイズや余白など）は、その選択根拠を解釈へ記録する。

命名のaudio記述は取得時にCodexが適用する。CLI内部のbatch番号は工程追跡用のIDで、シートの保存名を上書きしない。素材/音声保存先や引継ぎ先もCodexがシートから読み、利用者の権限・指示を確認して扱う。

## 時間と検証

fpsはFractionで保持する。絶対フレーム境界Fのサンプル位置を`round-half-up(F × 48000 × den / num)`で求める。行のサンプル長は両境界の差。末尾秒はフレーム数へ切上げ、最終行Hと総尺に一度含める。Hは行フレーム数×den/numの数値で書く。

音量目標はシート由来。計測許容差、内部PCM形式、AACのパディング/遅延、クリッピング防止は技術的検証条件。AAC後は目標LUFS±0.3、指定ピーク以下を確認する。末尾無音は符号化の影響として先頭1フレームを除外して測る。指定末尾が0〜1フレームなら測定可能な末尾区間がないことをreportへ明記する。

対応できないfps/コーデック/効果/範囲や、必須値が解決できない場合は停止する。未知キーを無視したり旧台本の値で補ったりしない。実行plan・音声バッチ・prepared・receiptは同じ規則へ結び付け、変更したシートを読み直す。
