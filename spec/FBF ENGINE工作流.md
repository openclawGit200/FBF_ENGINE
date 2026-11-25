# FBF ENGINE 工作流規格

角色提交交件資料。引擎驗證目前階段、版本、契約和回執後，依合法轉移推進狀態並派工。`python fbf.py contract` 列出工單階段與欄位摘要。角色可用 `fbflow_read` 查閱該工單或 ISSUE 的詳細交件契約。

工單主流程為 `backlog → design → acceptance_design → build → verify → adjudicate → closed`。重工可回到解法設計、驗收設計或施工。解法重工依序經過 `rework_design → rework_acceptance_design → rework_build → verify`。

ISSUE 流程為 `raised → designing → handling → building → adjudicating → closed`。由原發起者在裁決階段提交處理結果。達到退回上限時，停下來等待人類裁決。

0.加入引擎（前置）：一個專案要先加入FBF ENGINE才有正本與角色席位。
兩條路，結果一樣（同一份 join 邏輯）：
	(a)人在終端機跑 `fbf.py join --project <專案路徑>`；
	(b)session在專案目錄下呼叫MCP工具 `fbflow_project_join`——
	   建正本＋三個角色席位＋寫專案層MCP設定（帶FBF_PROJECT_ROOT），
	   並把這個session綁到該專案，呼叫後不用重開連線。
只能加入目前工作目錄或它的子目錄；引擎的安裝目錄不放專案資料。

### 需求變更：已確認需求不可修改，工單一旦發出也不可修改。
	需求變了 → 由USER新增一條需求（requirement confirm --supersedes 舊需求 id），
	PLANNER照新需求開新工單（fbflow_work_create），
	再用 fbflow_work_supersede 把舊工單標成「已被取代」：
	舊工單的內容與狀態不動，只停工、不再派工、交件會被擋。
	舊需求底下的工單也要一併標成已取代，否則會被派工做白工，專案也結不了案。

1.PLANNER接到任務後，先在FBF ENGINE註冊，然後提醒USER完善角色表，產生
提示WORKER跟THINKER兩個SESSION註冊FBF ENGINE，填寫角色表的提示詞
然後根據sharpen，跟GRILL ME的方法跟精神，把任務邊界，產出，要達成的目的等不明確的地方
拷問到明確，覺得明確後根據in-your-own-words的方法跟精神，把任務邊界，產出，要達成的目的等複述一遍給USER確認
USER確認無誤後，然後根據搭配使用的專案SKILL跟figure-it-out的精神
產出可稽核的執行計畫，然後把這個計畫，依據三句話就要能描述完畢的原則，分拆成多個工作
挑出最值錢的或是最優先的任務給 THINKER
對應 pstack ：sharpen，figure-it-out，in-your-own-words
工單狀態：待派工

2.thinker 接到工單後，根據搭配的專案 SKILL，在同一棒內直接產出至少兩個解法（不分派 SUBAGENT、不帶 context id）
兩案不得實質相同：成本與修改範圍不能都一樣
對應 pstack ：architect，how、why
工單狀態：解法設計中

3.PLANNER 挑選 THINKER 提供的解法、從兩個挑一個成本比較低，工程量比較小的，以案號記錄選案（selection.solution_id 填案號，例如 "1"），根據專案SKILL設計驗收方式，發出工單給到 WORKER
依專案 SKILL 設計驗收方式
工單狀態：驗收方式設計中

4.WORKER 接到工單以後，根據專案 SKILL ，工單要求，設計的解法以及驗收方法開始施工
	a.完成任務後根據專案 SKILL、驗收方法 進行自檢後，透過 MCP 交件，由引擎推進至驗收並派給 THINKER
	對應 pstack ：tdd

	b.WORKER 在同一個問題上連撞三次，填寫ISSUE單，發給THINKER，由THINKER在同一棒內直接產出至少兩個解法（不分派 SUBAGENT），
	交由PLANNER從兩個挑一個成本比較低，工程量比較小的，以案號記錄選案，後在 ISSUE 單上填寫對策，通知WORKER 依據對策執行，如果對策生效，問題解決，ISSUE 關閉，如果對策未生效
	在同一張 ISSUE單上反應發生的情況，重複上一個流程，連續兩次ISSUE單 未能結案，流程停下來等人類裁決

工單狀態：施工中

5 THINKER接到驗收通知後，根據驗收方式進行驗收，填寫驗收說明跟是否通過，發給PLANNER做最後判斷
對應 pstack：interrogate、thermos
工單狀態：驗收中

6.PLANNER根據WORKER的產出，THINKER的驗收說明跟驗收結果判斷是否這張工單結案
PLANNER 提交結案或重工裁決。引擎依驗收結果與重工類型結案或派回指定階段。
工單狀態：結案中

7.THINKER根據PLANNER填寫要重工的部分，根據搭配的專案 SKILL，在同一棒內直接產出至少兩個解法（不分派 SUBAGENT），發給PLANNER
對應 pstack ：architect，how、why
工單狀態：重工解法設計中

8.PLANNER 挑選 THINKER 提供的重工解法、從兩個挑一個成本比較低，工程量比較小的，以案號記錄選案（selection.solution_id 填案號），根據專案SKILL設計驗收方式，發出工單給到 WORKER
依專案 SKILL 設計驗收方式
工單狀態：重工驗收方式設計中

9.WORKER 接到工單以後，根據搭配的專案 SKILL ，工單要求，設計的解法以及驗收方法開始重工
完成後根據專案 SKILL、驗收方法進行自檢後，透過 MCP 交件，由引擎推進至驗收並派給 THINKER
對應 pstack ：tdd
工單狀態：重工中

然後再度循環流程5~9，如果到重工二工單還是不能結案，就根據沒辦法完成的部分，判斷能不能繞過，如果可以就重新設計工作繞過沒辦法完成的部分，如果繞不過就停工等人類

10.循環以上流程，直到所有工作都標記完成，除非遇到需要停止等人類

專案文件設計
1.角色表：每個專案一張，專案由PLANNER啟動時，PLANNER在這張表上記錄人類提供的專案組織結構
PLANNER固定一人，THINKER最少一人，WORKER最少一人
角色表包含：
	1.AGENT 名稱，比如是OPENCODE、PI、CODEX，CLAUDE CODE，DEEPSEEK HARNESS等，這個要用來提示之後要用甚麼指令發提示詞給這個SESSION進行溝通
	2.SESSION ID：用來辨識正確通知的對象
	3.角色：是 PLANNER、THINKER、WORKER 其中哪一個
	4.簡稱：用來作為一個角色有多個AGENT時的辨認
每個在專案中的SESSION啟動時，由人類指定角色跟簡稱，然後讀取角色表填寫或修改對應的角色、簡稱的SESSION ID、AGENT 名稱
每次要通知專案中其他AGENT時，要先讀取角色表，確定要通知的角色的AGENT 名稱SESSION ID
配合角色表設計程式：INFORM，裡面包含不同AGENT傳送提示詞的指令，比如PLANNER派工給WORKER，
讀取角色表，呼叫這隻程式，傳入要通知的SESSION跟提示詞，提示詞只含通知內容，比如"派工工單編號1234567，請讀取這張工單跟專案SKILL，進行開工"
提示詞內容不包含工作狀態（狀態由角色用 fbflow_read 查正本）；但提示詞要帶這一棒的交件清單
WORKER 依角色指引將產出放在自己的簡稱資料夾，並在交件中附上路徑、大小與 SHA-256。

2.工單：工單的資料正本是SQLite（fbf_engine.db），**不再生成工單資料夾**
（2026-11-24 裁決：多一份版本就多一份錯亂的機會；差異記錄見 DEVIATIONS.md D34）
工單設計精神如：工單設計.HTML
角色用 fbflow_read(target_kind=work) 讀正本快照（狀態、版本、解法、選案、驗收方式、產出、重工）
這一棒要交的欄位與規格由派工提示詞直接攜帶（來源與守衛同一份 workflow.stage_contract）
工單的解法表有「案號」欄位（1 起算），PLANNER 選案在 selection.solution_id 填案號


3.ISSUE單：ISSUE單的資料正本同樣是SQLite，**不再生成ISSUE資料夾**
（DEVIATIONS.md D34）
ISSUE單設計精神如：ISSUE單設計.HTML
角色用 fbflow_read(target_kind=issue) 讀正本快照；這一棒要交的欄位寫在派工提示詞裡
ISSUE單的解法表有「案號」欄位（1 起算），PLANNER 選案在 selection.solution_id 填案號



### 角色與交件權限

PLANNER 建立工單、選案、設計驗收與提交結案裁決。THINKER 提交解法及獨立驗收結果。WORKER 提交施工結果、自檢與產物指紋。引擎依目前階段檢查交件角色與欄位。

失敗計數器分成兩個層面
WORKER同一個問題連撞三次：這是技術上的問題由AGENT自行解決
ISSUE解決流程跑了兩輪解決不了ISSUE，這可能就涉及換目標了，所以要人類介入
這是避免在這邊無窮迴圈，有停下來的機制


