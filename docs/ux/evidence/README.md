# Browser evidence

All captures use isolated fake providers and temporary data. They contain no operational task data or live provider activity.

- [Regression result](pytest.txt): 290 passed.
- [Browser result](browser-results.json): 224 checks passed; 42 page/viewport checks; no JavaScript exceptions.
- [Desktop comparison](visual-comparison.jpg): approved source left, production right.
- [Mobile comparison](mobile-comparison.png): approved source left, production right.

| Production view | 1440px | 768px | 390px |
| --- | --- | --- | --- |
| tasks | [Screenshot](1440-tasks.png) | [Screenshot](768-tasks.png) | [Screenshot](390-tasks.png) |
| storyboard | [Screenshot](1440-storyboard.png) | [Screenshot](768-storyboard.png) | [Screenshot](390-storyboard.png) |
| review | [Screenshot](1440-review.png) | [Screenshot](768-review.png) | [Screenshot](390-review.png) |
| task-detail | [Screenshot](1440-task-detail.png) | [Screenshot](768-task-detail.png) | [Screenshot](390-task-detail.png) |
| gpu | [Screenshot](1440-gpu.png) | [Screenshot](768-gpu.png) | [Screenshot](390-gpu.png) |
| offers | [Screenshot](1440-offers.png) | [Screenshot](768-offers.png) | [Screenshot](390-offers.png) |
| rental | [Screenshot](1440-rental.png) | [Screenshot](768-rental.png) | [Screenshot](390-rental.png) |
| comfyui | [Screenshot](1440-comfyui.png) | [Screenshot](768-comfyui.png) | [Screenshot](390-comfyui.png) |
| workflow | [Screenshot](1440-workflow.png) | [Screenshot](768-workflow.png) | [Screenshot](390-workflow.png) |
| workflow-blocked | [Screenshot](1440-workflow-blocked.png) | [Screenshot](768-workflow-blocked.png) | [Screenshot](390-workflow-blocked.png) |
| monitor | [Screenshot](1440-monitor.png) | [Screenshot](768-monitor.png) | [Screenshot](390-monitor.png) |
| series | [Screenshot](1440-series.png) | [Screenshot](768-series.png) | [Screenshot](390-series.png) |
| series-bible | [Screenshot](1440-series-bible.png) | [Screenshot](768-series-bible.png) | [Screenshot](390-series-bible.png) |
| settings | [Screenshot](1440-settings.png) | [Screenshot](768-settings.png) | [Screenshot](390-settings.png) |
| run-output | [Screenshot](1440-run-output.png) | [Screenshot](768-run-output.png) | [Screenshot](390-run-output.png) |

Additional states: [create task](1440-create-task.png), [invalid Brief](1440-task-error.png), [search empty](1440-task-search-empty.png), [provisioning](1440-gpu-provisioning.png), [destroy unknown](1440-gpu-unknown.png), [run queued/running](1440-run-running.png), [output download failure](1440-run-output-failed.png), [mobile navigation](390-navigation-open.png), [offer connection error](390-offer-network-error.png), [no offers](390-offer-empty.png).

Approved source captures use `WIDTH-reference-VIEW.png`; they are retained at the same widths for Tasks, Storyboard, GPU, rental, ComfyUI, workflow, Monitor, Series and Settings. The source uses demo arrays and illustrations; production uses persisted test records and actual fake-provider media. Review/source differences are documented in [verification](../verification.md).

The full-page mobile captures place the fixed bottom navigation at the viewport boundary within each tall screenshot. Tables with long audit/manifest/review data scroll inside their own region. The document itself does not scroll horizontally.
