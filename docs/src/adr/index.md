---
id: ADR-INDEX
title: Decision register (ADRs)
type: adr_index
ver: 1.0
status: final
date: 2026-10-19
owner: Tuan
rev:
  - [1.0, 2026-10-19, Generated from the ADRs]
---

<!-- id:summary -->
## Overview

<!-- id:summary.b1 -->
```kpi
ADRs | 3
Pending | 3
Approved | 0
One-way | 3
Nearest due | 2026-10-26
```

<!-- id:list -->
## ADR list

<!-- id:list.b1 -->
Table: ADR list
| ADR | Decision | Impact | Door | Cost per month | Due | Status |
| --- | --- | --- | --- | --- | --- | --- |
| [ADR-0001](0001-generic-fan-out.md) | Module khai báo fan_out trỏ tới một danh sách trong contract; runner không viết cứng "theo scene". | High | One-way | 0 USD | W1 · 2026-10-26 | Pending approval |
| [ADR-0002](0002-speechscript-contract.md) | voice đọc SpeechScript (danh sách đoạn id, text, speaker) thay vì ScenePlan; mỗi quy trình tự sinh SpeechScript. | High | One-way | 0 USD | W1 · 2026-10-26 | Pending approval |
| [ADR-0003](0003-parallel-step-groups.md) | Profile chấp nhận nhóm parallel ngay từ Phase 1; runner được phép chạy các bước trong nhóm lần lượt cho tới khi cần nhanh hơn. | Medium | One-way | 0 USD | W1 · 2026-10-26 | Pending approval |

<!-- id:decide -->
## Recorded decisions

<!-- id:decide.b1 -->
```chk
- [ ] ADR-0001 · Module khai báo fan_out trỏ tới một danh sách trong contract; runner không viết cứng "theo scene".
  - Approve
  - Reject
  - Request changes
- [ ] ADR-0002 · voice đọc SpeechScript (danh sách đoạn id, text, speaker) thay vì ScenePlan; mỗi quy trình tự sinh SpeechScript.
  - Approve
  - Reject
  - Request changes
- [ ] ADR-0003 · Profile chấp nhận nhóm parallel ngay từ Phase 1; runner được phép chạy các bước trong nhóm lần lượt cho tới khi cần nhanh hơn.
  - Approve
  - Reject
  - Request changes
```

<!-- id:risk-matrix -->
## Risks

<!-- id:risk-matrix.b1 -->
Table: Risk matrix
| Likelihood \ Impact | Low | Medium | High |
| --- | --- | --- | --- |
| High | – | – | – |
| Medium | R1 | R1 | – |
| Low | R2 | R2 · R2 | – |
| Not rated | R3 | R3 · R3 | R1 |

<!-- id:risk-matrix.b2 -->
Table: Risk register
| ID | Risk | Likelihood | Impact | Mitigation | Owner | Source |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | Viết cứng "theo scene" lọt vào runner qua đường tắt | Not rated | High | Test runner với một contract giả không có scene | Claude | ADD-AIVIDEO-001 12.4 |
| R2 | Một module cần fan-out lồng nhau (chương × đoạn) | Low | Medium | Làm phẳng danh sách trong contract, id ghép `ch01_p003` | Claude | Thiết kế này |
| R3 | Đổi id phần tử giữa các lượt làm mất liên kết lượt thử | Not rated | Medium | id ổn định do module sinh, không theo vị trí | Claude | Thiết kế này |
| R1 | Narration trong ScenePlan và text trong SpeechScript lệch nhau sau khi sửa | Medium | Medium | Video: SpeechScript sinh lại từ ScenePlan khi sửa scene; đoạn dùng cùng `id` với scene | Claude | Thiết kế này |
| R2 | Phụ đề cần mốc theo scene, không theo đoạn | Low | Low | Với video, một đoạn ứng với một scene (`id` trùng) | Claude | ADD-AIVIDEO-001 4.6 |
| R3 | Audiobook cần thêm trường (chương, cảm xúc) | Not rated | Low | Thêm trường tùy chọn, tăng `schema_version` khi không tương thích | Claude | Thiết kế này |
| R1 | Hai bước trong nhóm dùng chung rate limit một provider | Medium | Low | `concurrency` theo provider, không theo bước | Claude | ADD-AIVIDEO-001 mục 5 |
| R2 | Gate nằm trong nhóm làm trạng thái khó hiểu | Low | Medium | Validation cấm `gate` bên trong nhóm | Claude | Thiết kế này |
| R3 | SQLite bị ghi đồng thời nhiều hơn khi chạy song song thật | Not rated | Medium | WAL; ghi ngắn; đo trước khi bật | Claude | ADD-AIVIDEO-001 mục 10 |

<!-- id:lifecycle -->
## Status lifecycle

<!-- id:lifecycle.b1 -->
```d2
# cap: ADR status lifecycle
direction: right
draft: Draft
pending: Pending approval {class: gate}
approved: Approved {class: hands}
changes: Changes requested {class: data}
rejected: Rejected {class: bad}
superseded: Superseded
draft -> pending
pending -> approved
pending -> rejected
pending -> changes
changes -> pending: resubmit
approved -> superseded
```
<picture>
<source media="(prefers-color-scheme: dark)" srcset="index.figs/lifecycle.b1-dark.svg">
<img alt="ADR status lifecycle" src="index.figs/lifecycle.b1-light.svg">
</picture>

<!-- id:lifecycle.b2 -->
Table: ADR states
| State | Meaning | Next step |
| --- | --- | --- |
| Draft | Being written, not yet sent to the decision authority | Complete the manager part, move to Pending approval |
| Pending approval | Sent to the decision authority, with a due week | The decision authority responds before the due date |
| Approved | In force, being implemented | Review on the review date |
| Rejected | Not applied; the reason is recorded | Propose another option if needed |
| Changes requested | The decision authority requests changes | Revise per the note, resubmit as Pending approval |
| Superseded | A newer ADR replaces this decision | Link to the newer ADR |

<!-- id:process -->
## Feedback process

<!-- id:process.b1 -->
```d2
# cap: Decision feedback process
shape: sequence_diagram
authority: Decision authority {class: gate}
kit: Report Kit {class: brain}
authority -> authority: Review decision records
authority -> kit: Authorized feedback text
kit -> kit: Update decisions and history
kit -> kit: Rebuild register source
kit -> authority: Updated records for review
```
<picture>
<source media="(prefers-color-scheme: dark)" srcset="index.figs/process.b1-dark.svg">
<img alt="Decision feedback process" src="index.figs/process.b1-light.svg">
</picture>

<!-- id:process.b2 -->
`report adr-apply <adr-dir> <feedback file>` reads the feedback text copied from the register or an ADR page. Each line with an ADR ID and the choice Approve, Reject or Request changes updates that ADR's `adr` front matter: state, decision date, reason (the attached note, if any), and adds a version to the history; the register is then rebuilt.

<!-- id:scale -->
## Rating scales

<!-- id:scale.b1 -->
Table: Rating scales
| Attribute | Values | Rule |
| --- | --- | --- |
| Operational impact | High · Medium · Low | Record operational impact according to the host policy and supporting evidence. |
| Door type | One-way · Two-way | The rule is set by the host project policy. |
| Risk likelihood | Not rated · Low · Medium · High | Rated only from a measurement or a documented estimate; until then Not rated. Use the host project evidence and rating policy. |
| Risk impact | Low · Medium · High | The rule is set by the host project policy. |
| Decision due | Week W with a positive number | Use the host project schedule for dependent work. |
| Review date | Date | Set according to the host project review policy. |
