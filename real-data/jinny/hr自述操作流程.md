在“MFG Hiring Progress”表中的“MFG Analysis” sheet新建数据透视表1，命名为部门剩下的HC，数据源是“Dashboard Data”表中的“Sheet1” 

数据透视表的筛选项为： 

Dept. 筛选为MFG 

Candidate Name筛选为 空白 

Location筛选为	Zhuhai 

Offer Accepted Date 不筛选 

在“MFG Hiring Progress”表中的“MFG Analysis” sheet新建数据透视表2，命名为部门已经完成的HC，数据源是“Dashboard Data”表中的“Sheet1” 

数据透视表的筛选项为： 

Dept. 筛选为MFG 

Location筛选为	Zhuhai 

Offer Accepted Date 筛选为 上周五到这周四，例如10月16日到10月22日这段时间的日期（包含10.16&10.22） 

在“MFG Hiring Progress”表中的“MFG Analysis” sheet新建数据透视表3，命名为部门本周入职HC，数据源是“Dashboard Data”表中的“Sheet1” 

数据透视表的筛选项为： 

Dept. 筛选为MFG 

Location筛选为	Zhuhai 

Onboarding Date 筛选为 上周五到这周四，例如10月16日到10月22日这段时间的日期（包含10.16&10.22） 

以上三个表的数据总结到“MFG Hiring Progress”表中的“MFG Analysis”sheet的总表中（Z-AD列） 

在“MFG Hiring Progress”表中的“MFG Hiring Progress”sheet中，将1到9行的数据复制黏贴到10列，原来的10以下行数自动往下挪，并且在9行和10行中间加两行 

将“MFG Hiring Progress”表中的“MFG Analysis”sheet的总表中AC列offered数量根据部门加到同表中“MFG Hiring Progress”sheet的C2-C8 

将“MFG Hiring Progress”表中的“MFG Analysis”sheet的总表中AD列offered数量根据部门加到同表中“MFG Hiring Progress”sheet的D2-D8 

将“MFG Hiring Progress”表中“MFG Hiring Progress”sheet的C2-C8根据部门减去同表中“MFG Analysis”sheet的总表中AD列数据 

检查“MFG Hiring Progress”表中“MFG Hiring Progress”sheet的E列是否与同表中“MFG Analysis”sheet的总表中AB列数据一致（按照部门核对），不一致则在该单元格标记黄色 

查阅“Recruitment Data-v1 ”表，该表中”Onboarding List”sheet中I列Offer Date筛选从上周五到这周四（例如10月16日到10月22日这段时间的日期，包含10.16&10.22），这些人按照C列筛选MFG，根据D列部门与“MFG Hiring Progress”表中“MFG Hiring Progress”sheet的A列部门来返回该部门B列的数值，返回的数值放在G2-G8，清空原来G2-G8的数据，返回形式为：B列数值总和+“已发offer“，“Recruitment Data-v1 ”表中”Onboarding List”sheet中C列“Passive MFG Office”计入MFG Office 

查阅“Recruitment Data-v1 ”表，该表中”Onboarding List”sheet中F列报道日期筛选从上周五到这周四（例如10月16日到10月22日这段时间的日期，包含10.16&10.22），这些人按照C列筛选MFG，根据D列部门与“MFG Hiring Progress”表中“MFG Hiring Progress”sheet的A列部门来返回该部门B列的数值，返回的数值放在G2-G8，不要覆盖原来G2-G8的数据，在原来基础上增加，返回形式为：B列数值总和+“已入职“，“Recruitment Data-v1 ”表中”Onboarding List”sheet中C列“Passive MFG Office”计入MFG Office 

查阅“Recruitment Data-v1 ”表，该表中”Onboarding List”sheet中F列报道日期筛选从上周五到这周四（例如10月16日到10月22日这段时间的日期，包含10.16&10.22），再在这些数据中筛选标记灰色的行，这些人按照C列筛选MFG，根据D列部门与“MFG Hiring Progress”表中“MFG Hiring Progress”sheet的A列部门来返回该部门B列的数值，返回的数值放在G2-G8，不要覆盖原来G2-G8的数据，在原来基础上增加，返回形式为：B列数值总和+“拒offer“，“Recruitment Data-v1 ”表中”Onboarding List”sheet中C列“Passive MFG Office”计入MFG Office 

清空“MFG Hiring Progress”表中“MFG Hiring Progress”sheet F2-F8数据，在“Recruitment Data-v1 ”表中“Applicant list - 20260501“C列筛选标记”fff2cc“的行，然后根据H列计算各个部门的数量，“生产经理”计入MFG Office，并将数值根据部门返回“MFG Hiring Progress”表中“MFG Hiring Progress”sheet 的F2-F8 

在“Recruitment Data-v1 ”表中“Applicant list - 20260501“K列筛选日期在当天及以后的行，根据H列计算各个部门的数量，“生产经理”计入MFG Office，并将数值根据部门返回“MFG Hiring Progress”表中“MFG Hiring Progress”sheet 的F2-F8，不要覆盖原来F2-F8的数据，在原来基础上增加 

在“Recruitment Data-v1 ”表中“Applicant list - 20260501“Q列筛选日期在当天及以后的行，根据H列计算各个部门的数量，“生产经理”计入MFG Office，并将数值根据部门返回“MFG Hiring Progress”表中“MFG Hiring Progress”sheet 的F2-F8，不要覆盖原来F2-F8的数据，在原来基础上增加 

在“MFG Hiring Progress”表中“MFG Hiring Progress”sheet中复制第二行到第九行最后一列到下一列，并将新增的这列第一行标题改为：Selected Candidates-日期(上周五到这周四,10.16-10.22) 

在“Recruitment Data-v1 ”表中“Applicant list - 20260501“C列筛选日期从上周五到这周四（例如10月16日到10月22日这段时间的日期，包含10.16&10.22）的行，根据H列计算各个部门的数量，“生产经理”计入MFG Office，并将数值根据部门返回“MFG Hiring Progress”表中“MFG Hiring Progress”sheet 的第二行到第九行的最后一列，覆盖原来数据 