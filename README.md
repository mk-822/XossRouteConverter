# XOSS NAV+ ルート転送ツール

GPXを読み込み、XOSS NAV+用の `routebooks.json` と `Routes\*.ro` を作成して、USBマスストレージへ転送するWindows向けGUIです。

## 起動

`run_xoss_route_converter.bat` をダブルクリックしてください。Python 3とTkinterが必要です。Windows公式のPythonインストーラーでは通常Tkinterが含まれます。

Python不要で使う場合は、`build_exe.bat` を一度実行してください。`dist\XOSS_NAV_Route_Converter.exe` が単体EXEとして生成されます。生成後はEXEだけを別フォルダへコピーして起動できます。

## 使い方

1. GPXを選択します。
2. ルート名と1ルートあたりの上限距離を設定します。初期値は300kmです。
3. プレビューで全長、分割数、各区間の距離・点数・獲得標高を確認します。
4. XOSS NAV+をUSB接続し、`routebooks.json` があるドライブを再検索します。
5. `XOSS NAV+へ転送` を押します。

転送時は既存ルートを必ず保持し、新しいRIDのルートを追記します。元の `routebooks.json` は同じドライブに日時付きのバックアップとして残します。転送完了後は端末内ルート一覧を自動更新します。

端末接続中は「端末内ルート管理」に既存ルートを一覧表示できます。複数選択して削除すると、削除前に `XOSS_Backup_日時` フォルダへ `routebooks.json` と対象ROを退避します。

## 形式について

XOSS NAV+のRO形式は公開されたGPX変換仕様ではないため、同梱の `assets\xoss_nav_template.ro` をXOSS NAV+の参照データとして使い、XZRoutes v2の構造を保ったまま経路・距離・標高・名前・CRCを差し替えています。実機での表示確認は、NAV+本体を接続してRoutebookから各ルートを選択して行ってください。
