サンライズ出雲 GitHub Actions 監視版

公開してよいファイル:
- sunrise_watch_actions.py
- requirements.txt
- .github/workflows/sunrise-watch.yml

GitHub Secrets に入れるもの:
- NTFY_TOPIC_URL
- TRIP_LABEL
- URL_SINGLE_DELUXE
- URL_SINGLE_TWIN
- URL_SINGLE
- URL_SOLO
- URL_SUNRISE_TWIN

重要:
- URLやntfyトピックはソースコードに書かれていません。
- workflowは schedule と workflow_dispatch だけです。
- pull_request / pull_request_target / push では動きません。
- 01:30～05:30 JSTはscheduleを設定していません。
- 手動実行を01:30～05:30に行っても、Python側でもe5489チェックを止めます。
- stateキャッシュには通知済みフラグ等だけを保存し、Secretは保存しません。
