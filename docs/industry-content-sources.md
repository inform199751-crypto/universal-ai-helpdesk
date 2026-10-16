# 示範行業知識庫的官方來源

電商(好日子生活選物)與診所(晴日皮膚科診所)的 FAQ、政策,凡是 2026-10-16
這一輪新增或修改的條目,都列在下面:依據的事實編號、那則事實引用的官方網址、查閱日。
兩家店都是虛構的;寫進 YAML 的句子是我們自己改寫的,沒有照抄任何網站。

- 事實編號(E1–E18、C1–C18)是研究底稿裡的編號;底稿是本機工作檔,不進版控,所以這份文件把每則事實用到的網址都直接列出來。
- 「官方來源」只列底稿裡實際開過的頁面。全部查閱日都是 **2026-10-16**。
- 店家自己的營運承諾(990 元免運、三到五個工作天退款、統編填備註欄、未滿十八歲要陪同)
  不是法規,沒有官方來源,也不在這份清單裡。
- 回歸測試 `tests/test_industries.py::test_faq_and_policies_do_not_contradict_official_rules`
  擋住兩句已知跟法規衝突的舊說法:「非瑕疵退貨運費由買方負擔」、「病歷須由本人親自辦理」。

## 電商:好日子生活選物

| 條目 | 依據 | 官方來源 |
|---|---|---|
| FAQ「可以退貨嗎」(改) | E1、E2、E5 | [消保法§19](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=J0170001&flno=19)、[消保處:七日怎麼起算](https://cpc.ey.gov.tw/Page/4432D6D5FA6677B9/c25817a4-de69-4835-a81a-ebd05a19669b)、[消保處:網購運費由誰負擔](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/d3b2324c-f465-45ec-a52e-1daa3b050c41)、[消保處:可否不提供七日退貨](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/f8e15c56-0cbe-49ac-a7ba-2f41e1d9ca55) |
| FAQ「拆封或用過還能退嗎」(新) | E3 | [消保處:七日內拆封或使用](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/2055825e-e262-47da-9f56-398a71c92c5b) |
| FAQ「哪些商品不能七天內退貨」(新) | E4、E5 | [合理例外情事適用準則](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0170012)、[消保處:可否不提供七日退貨](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/f8e15c56-0cbe-49ac-a7ba-2f41e1d9ca55)、[消保處:只有到貨三天內可退換](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/262317a7-5733-4bd2-8599-5ad74a7b0009) |
| FAQ「可以選超商取貨嗎」(改) | 底稿「依店家而異」一節 | 無:超商保管天數找不到官方來源,所以拿掉原本的「七天內領取」,改成依通知的期限 |
| FAQ「退款多久會到帳」(改) | E6 | [消保法§19-2](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=J0170001&flno=19-2) |
| FAQ「刷卡付款的訂單也能退嗎」(新) | E7 | [消保處:刷卡購物商品有瑕疵](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/a280669e-411c-4d1f-a600-28e10120bf2e) |
| FAQ「發票怎麼拿」(改) | E12 | [電子發票實施作業要點](https://law-out.mof.gov.tw/LawContent.aspx?id=FL041411) |
| FAQ「可以存手機載具或捐贈發票嗎」(新) | E12、E13 | [電子發票實施作業要點](https://law-out.mof.gov.tw/LawContent.aspx?id=FL041411) |
| FAQ「超過七天才發現瑕疵怎麼辦」(新) | E7 | [消保處:刷卡購物商品有瑕疵](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/a280669e-411c-4d1f-a600-28e10120bf2e)、[消保處:七日解除權例外規定施行](https://cpc.ey.gov.tw/Page/53D79214534B3D4C) |
| FAQ「商品標錯價怎麼辦」(新) | E9(應記載事項第 3 點)、E11 | [網路交易定型化契約應記載及不得記載事項 PDF](https://www.ey.gov.tw/File/2D44FB65E5FF31C9)、[消保處:網站標價錯誤](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/9a29362d-d842-4a4d-86f5-0f32ab25438b)、[應記載事項第 5 點確認機制函釋](https://www.ey.gov.tw/Page/8F51FEE53AB45EA9/938e77ff-f5d7-4aa0-8b52-956d021b79ea) |
| FAQ「換貨要另外付運費嗎」(改) | E1、E2 | [消保法§19](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=J0170001&flno=19)、[消保處:網購運費由誰負擔](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/d3b2324c-f465-45ec-a52e-1daa3b050c41) |
| FAQ「對訂單或服務有意見要怎麼反映」(新) | E8、E9(應記載事項第 14 點) | [消保法§18](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=J0170001&flno=18)、[應記載及不得記載事項 PDF](https://www.ey.gov.tw/File/2D44FB65E5FF31C9) |
| FAQ「可以查詢或刪除我的個人資料嗎」(新) | E15、E16、E17 | [個資法§3](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=I0050021&flno=3)、[個資法§13](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=I0050021&flno=13)、[個資法§11](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=I0050021&flno=11) |
| FAQ「怎麼停止收到行銷訊息」(新) | E18 | [個資法§20](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=I0050021&flno=20)、[拒絕商業行銷指引 PDF](https://ws.pdpc.gov.tw/FS01/FilePath/3/relfile/33/194/3d7cd346-19f7-4952-bd6b-5a8b2b5bcb1b.pdf) |
| 政策「退換貨」(改) | E1–E5 | 同上面退貨、拆封、例外商品三列 |
| 政策「退款」(改) | E6 | [消保法§19-2](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=J0170001&flno=19-2) |
| 政策「運費」(改) | E2 | [消保處:網購運費由誰負擔](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/d3b2324c-f465-45ec-a52e-1daa3b050c41) |
| 政策「發票」(改) | E12、E13、E14 | [電子發票實施作業要點](https://law-out.mof.gov.tw/LawContent.aspx?id=FL041411) |
| 政策「個資」(改) | E15–E18 | 同上面個人資料、行銷訊息兩列 |
| 術語「鑑賞期」(改) | E1、E3 | [消保處:七日怎麼起算](https://cpc.ey.gov.tw/Page/4432D6D5FA6677B9/c25817a4-de69-4835-a81a-ebd05a19669b)、[消保處:七日內拆封或使用](https://cpc.ey.gov.tw/Page/BC16ACF0BBB9CCC2/2055825e-e262-47da-9f56-398a71c92c5b) |

## 診所:晴日皮膚科診所

只寫行政資訊,不放疾病、症狀、用藥或療效內容。

| 條目 | 依據 | 官方來源 |
|---|---|---|
| FAQ「初診要帶什麼」(改) | C10 | [全民健康保險醫療辦法§3](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0060009&flno=3) |
| FAQ「忘了帶健保卡怎麼辦」(新) | C11 | [全民健康保險醫療辦法§4](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0060009&flno=4)、[同辦法§5](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0060009&flno=5) |
| FAQ「健保卡遺失怎麼辦」(新) | C12 | [我的E政府:申請換發健保卡](https://www.gov.tw/News_Content_2_395888)、[衛福部:健保卡遺失或毀損怎麼申請](https://www.mohw.gov.tw/cp-16-81338-1.html) |
| FAQ「掛號費多少錢」(新) | C5 | [衛福部:停止適用掛號費參考範圍](https://www.mohw.gov.tw/cp-16-77894-1.html)、[醫事司:同一公告](https://dep.mohw.gov.tw/DOMA/cp-2708-77903-106.html) |
| FAQ「看健保要付部分負擔嗎」(新) | C1、C2 | [健保法§43](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0060001&flno=43)、[健保署部分負擔範例說明 PDF](https://www.nhi.gov.tw/ch/dl-6332-d1ba0e9d2ee346b3a974f97268a1becc-1.pdf)、[衛福部:111 年部分負擔調整](https://www.mohw.gov.tw/cp-16-67980-1.html)、[衛福部:112 年部分負擔調整](https://www.mohw.gov.tw/cp-6563-74962-1.html) |
| FAQ「哪些人可以免部分負擔」(新) | C4 | [衛福部:112 年部分負擔調整](https://www.mohw.gov.tw/cp-6563-74962-1.html)、[衛福部:調整方案預告](https://www.mohw.gov.tw/cp-16-74668-1.html)、[衛福部:111 年部分負擔調整](https://www.mohw.gov.tw/cp-16-67980-1.html) |
| FAQ「自費項目大概多少錢」(改) | C8 | [醫療法§21](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=21)、[醫療費用收費標準核定作業參考原則 PDF](https://www.mohw.gov.tw/dl-88112-a55c823c-3a65-4be8-bda9-b55ad8553926.html) |
| FAQ「收費會開收據嗎」(新) | C7 | [醫療法§22](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=22)、[衛福部:民眾自費相關管理措施](https://www.mohw.gov.tw/fp-3160-25375-1.html) |
| FAQ「醫美諮詢當天可以直接做嗎」(新) | C9 | [衛福部:修正美容醫學同意書範本](https://www.mohw.gov.tw/cp-2630-18828-1.html) |
| FAQ「就診資料會保密嗎」(新) | C15 | [醫療法§72](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=72)、[個資法§6](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=I0050021&flno=6) |
| 政策「初診資料」(改) | C10 | [全民健康保險醫療辦法§3](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0060009&flno=3) |
| 政策「自費項目」(改) | C7、C8 | [醫療法§22](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=22)、[醫療費用收費標準核定作業參考原則 PDF](https://www.mohw.gov.tw/dl-88112-a55c823c-3a65-4be8-bda9-b55ad8553926.html) |
| 政策「病歷與個資」(改) | C13、C14 | [醫療法§71](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=71)、[醫療法§70](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=70)、[病歷資料調閱授權書範本](https://www.mohw.gov.tw/cp-2657-6342-1.html) |
| 轉真人話術 privacy(改,觸發字沒動) | C13 | [醫療法§71](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=71) |
| 禁語加「根治」「百分之百」「最便宜」 | C16、C17、C18 | [醫療法§85](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=85)、[§86](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=86)、[§87](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=87)、[§103](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=103)、[§61](https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0020021&flno=61) |

## 刻意不寫具體數字的項目

這些數字有的在底稿裡查得到,但會因店家、縣市或年度而變,寫死在示範店裡反而會誤導客人。

- 電商:超商取貨的保管天數;信用卡退刷的入帳天數;個資申請的法定回覆天數(答案只寫「法定期限內」,
  不讓 bot 自己承諾天數);拆封試用後要負擔多少價值減損;例外商品只舉兩個例子,不列完整清單;
  換款式或尺寸時新商品的寄送運費。
- 診所:門診基本部分負擔的金額(2026 年有沒有再調整沒能確認,醫療資源缺乏地區的金額也不同);
  藥品部分負擔的門檻、比例與上限;掛號費金額(全國沒有上限,由診所自訂);自費項目價格(縣市核定);
  病歷複製費(包括 2010 年的光碟參考價);健保卡補發的工本費、作業天數,以及補發期間可以看診的天數。

## 未驗證、所以沒寫的項目

- 七天(電商)與 15 天(退款)遇到週末、假日怎麼算:官方頁面沒說,答案只寫「收到商品隔天起算七天」。
- 換貨(不是解除契約)時新商品的寄送運費:官方沒有直接說明。所以保留店家的換貨服務,只寫七天內寄回原商品不用負擔任何費用(E2),新商品怎麼寄請客人洽客服,不寫運費。
- 電子發票中獎通知與兌獎細節、統編發票的規定:財政部電子發票平台的網頁打不開,沒有寫。
- 網路交易定型化契約 2026 年的修正草案是否定案:不確定,只引用 2016 年版本裡沒有爭議的條文。
- 診所檢驗、檢查的部分負擔(C3):只在單一頁面看過,沒有寫。
- 忘了帶健保卡時,「不含例假日」包不包括國定假日:頁面沒說清楚,答案加上「實際期限以櫃檯說明為準」。
- 病歷複製本能不能由他人代辦、要哪些文件:沒有找到條文,答案改成「代他人申請請先來電確認」。
- 美容醫學的年齡門檻:那份頁面是 2016 年發布(2021 年更新),跟現行成年年齡可能不一致,沒有寫。
- 診所的 LINE 官方帳號要不要另外報主管機關備查:沒有找到官方說明,沒有寫。
- 免部分負擔的完整名單(例如產檢):只寫衛福部頁面列出的大類,其餘請客人洽櫃檯。
