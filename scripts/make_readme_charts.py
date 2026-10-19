import os; os.makedirs('docs/img', exist_ok=True)
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, matplotlib.animation as anim, datetime as dt
S="#fcfcfb"; T1="#0b0b0b"; T2="#52514e"; GR="#e4e3df"; B="#2a78d6"; O="#eb6834"
plt.rcParams.update({"font.family":"DejaVu Sans","font.size":10,"axes.edgecolor":GR,"axes.labelcolor":T2,"xtick.color":T2,"ytick.color":T2})
d=["09-25","09-26","09-27","09-28","09-29","09-30","10-01","10-02","10-03","10-04","10-05","10-06","10-07","10-08","10-09","10-10","10-11","10-12","10-13","10-14","10-15","10-16","10-17","10-18","10-19"]
found=[116,117,117,118,119,118,119,121,119,118,116,119,120,122,122,123,123,123,127,129,129,130,129,129,215]
shown=[14,1,8,6,10,16,8,7,0,0,1,0,0,0,0,0,0,2,0,0,0,0,0,0,1]
x=[dt.date(2026,int(s[:2]),int(s[3:])) for s in d]
fig,(a1,a2)=plt.subplots(2,1,figsize=(9,5.6),sharex=True,gridspec_kw={"height_ratios":[1,1]},facecolor=S)
for a in (a1,a2):
    a.set_facecolor(S); a.grid(axis="y",color=GR,lw=0.8); a.set_axisbelow(True)
    for s in ("top","right"): a.spines[s].set_visible(False)
a1.plot(x,found,color=B,lw=2,marker="o",ms=4)
a1.set_title("Знайдено до фільтрів (5 джерел)",loc="left",color=T1,fontsize=11)
a1.annotate("19.10: усі сторінки видачі\n129 → 215",xy=(x[-1],215),xytext=(x[-8],190),color=T2,fontsize=9,arrowprops=dict(arrowstyle="-",color=T2,lw=0.8))
a1.set_ylim(0,240)
a2.bar(x,shown,color=O,width=0.7)
a2.set_title("Показано (пройшли всі фільтри)",loc="left",color=T1,fontsize=11)
a2.axvline(dt.date(2026,10,2)+dt.timedelta(hours=12),color=T2,ls="--",lw=0.8)
a2.text(dt.date(2026,10,3),13,"02.10: оцінка за повним текстом,\nстоп за рівнем у назві",color=T2,fontsize=9,va="top")
a2.text(x[0],15.5,"77 за 8 днів",color=T2,fontsize=9,va="bottom")
a2.text(dt.date(2026,10,12),5,"4 за 17 днів",color=T2,fontsize=9)
a2.set_ylim(0,18)
import matplotlib.dates as md
a2.xaxis.set_major_formatter(md.DateFormatter("%d.%m")); a2.xaxis.set_major_locator(md.DayLocator(interval=3))
fig.tight_layout(); fig.savefig("docs/img/month_found_vs_shown.png",dpi=150,facecolor=S)

# funnel gif
st=[("Знайдено",215),("Після дедуплікації за ID",189),("У вікні дат (картка)",48),("Після стопу за рівнем у назві",37),("Пройшли картку (етап 1)",17),("Оцінено за повним текстом (етап 2)",10),("Показано",1)]
note=["","−13 дублі, −13 уже подано","−141 старші за вчора","−11 Senior/Lead/… у назві","−20 Low або не та роль","7 не оцінено: ліміт 10","+3 «майже» (досвід 2 роки)"]
fig,ax=plt.subplots(figsize=(10,4.4),facecolor=S)
def draw(k):
    ax.clear(); ax.set_facecolor(S); ax.set_xlim(-200,330); ax.set_ylim(-0.6,len(st)-0.4); ax.invert_yaxis(); ax.axis("off")
    ax.set_title("Один запуск, 19.10.2026: від 215 вакансій до 1",loc="left",color=T1,fontsize=12)
    for i,(lab,v) in enumerate(st[:k]):
        ax.barh(i,v,color=O if i==len(st)-1 else B,height=0.62)
        ax.text(-6,i,lab,va="center",ha="right",color=T1,fontsize=9.5)
        ax.text(v+4,i,f"{v}"+(f"   {note[i]}" if note[i] else ""),va="center",color=T2,fontsize=9)
frames=list(range(1,len(st)+1))+[len(st)]*6
a=anim.FuncAnimation(fig,draw,frames=frames,interval=700)
a.save("docs/img/funnel_2026-10-19.gif",writer=anim.PillowWriter(fps=1.4),savefig_kwargs={"facecolor":S})

# --- оновлення розділу README ---
import re, pathlib
SECTION = """## Приклад реального запуску

<img src="docs/img/funnel_2026-10-19.gif" alt="Воронка одного запуску 19.10.2026: 215 → 1" width="640">

**Один запуск (19.10.2026):** 215 вакансій з 5 джерел. 26 з них відсіяно як уже показані або подані, 141 — як старші за вчора, 11 — через рівень у назві (Senior/Lead/…). Із 37, що лишилися, етап 1 (оцінка картки) пропустив 17. Етап 2 (повний текст) оцінив 10, решта 7 вперлися в ліміт. Результат: 1 показана вакансія і 3 у блоці «майже» (бракує лише досвіду). Вартість запуску $0.32.

<img src="docs/img/month_found_vs_shown.png" alt="Знайдено і показано по днях, 25.09–19.10.2026" width="640">

**Місяць даних (25.09–19.10.2026, 25 запусків):**

- **Більше показаних не означало кращих.** До 02.10 Python-версія показала 77 вакансій за 8 днів. Ручна перевірка 27.09 знайшла серед 8 з них 0 релевантних: модель бачила лише картку, де не видно формату й досвіду. Відтоді рішення «показати» приймається тільки за повним текстом, плюс детермінований стоп за рівнем у назві. Після цього — 4 показані за 17 днів.
- **Збої видно, і вони не тихі.** 06.10 і 10.10 запуск не прочитав жодного джерела (0/5). Крок 4 позначив це як «НЕ ВИКОНАНО», а повторний запуск того ж дня пройшов 5/5. Дедуплікація при цьому не «з'їла» жодної вакансії.
- **Вартість відома до центу.** З 11.10 кожен запуск пише вартість у `logs/costs.csv`: $0.22–0.31 при Haiku 4.5 + Sonnet 5.5. Звірка з балансом Console (8.05 → 5.77 за 8 днів) дала ≈$0.23 на запуск. Один день розійшовся на $0.72 — то був окремий прогін Telegram-модуля, а не помилка обліку.
- **Повнота видачі.** 19.10 скрапери перейшли з першої сторінки на всі: знайдено 129 → 215. Ліміт етапу 2 (10) відразу став вузьким місцем — 7 вакансій лишились неоціненими, тому `MAX_FULLTEXT_VACANCIES` піднято до 20.

Розбір першого 8-денного запуску (17–24.09) — **[docs/sample-run.md](docs/sample-run.md)**.
"""
p = pathlib.Path("README.md"); t = p.read_text(encoding="utf-8")
new, n = re.subn(r"## Приклад реального запуску\n.*?(?=\n## Архітектура)", SECTION.rstrip() + "\n", t, flags=re.S)
assert n == 1, "розділ не знайдено"
p.write_text(new, encoding="utf-8")
print("OK: docs/img/*.png|gif створено, README оновлено")