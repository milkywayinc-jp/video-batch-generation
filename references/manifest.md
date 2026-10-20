# 実行manifest（schema 2）

制作規則の所有者はシートから導くresolved_rules。editは実ファイル・実測境界・確認結果だけを持つ。古いschema 1は再生成する。

1. inspect：シートを読み、raw planとinterpretationの雛形を保存。
2. Codexが自然文を解釈し、resolve：出典と型を検証して実行plan・editを作成。
3. editを実素材と確認結果で埋め、prepare：フレーム/サンプル単位のpreparedを計測。

## edit

- schema_version / source_hash / resolved_rules_hash：生成時の値を保つ。
- fonts：ファミリー→ウェイト文字列→実ファイルの絶対パス。例：`{"指定フォント":{"400":"/path/regular.ttf","900":"/path/black.ttf"}}`。可変フォントなら同じファイルでもよい。staticフォントは正しいファミリー/ウェイトのファイルへ明示的に対応付ける。
- material_root：素材フォルダのローカルパス。素材名と一意に対応することを確認する。
- batches：id、調整済みWAVのpath、サンプル境界のboundaries、verified。境界は0から全サンプル終端まで、行数+1個の厳密増加整数。試聴し、全発話を台本順に一度ずつ割り当てる。
- cuts：row、video_path、video_start、source_intervals、source_verified、silent_frames、bgm_pathだけを持つ。
- source_intervals：元動画上の開始/終了秒ペア。発話・口・映像の連動を確認し、source_verifiedへ記録する。
- silent_frames：音声なしカットの根拠のある尺をフレームで指定する。一律の仮尺を使わない。

字幕、音声速度、BGMゲイン、末尾尺、fps等をeditへ重複して入れない。字幕位置の制作判断もシートの指定から解決し、rule側へ出典付きで記録する。

prepareは素材不足をまとめて返す。preparedは測定結果で、手編集して書戻しには使わない。ルール・plan・音声・依存ファイルのハッシュを検証する。

## 私有状態

setupの記録には基準版URL、コピーURL、トランザクションID、保存先の確認結果、制作チャット/手動引継ぎの状態が含まれる。writebackのjournalには自分が変更したセルと直前値を記録する。いずれも公開・Gitへの追加をしない。
