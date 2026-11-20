"""Four PokeBench results figures. Pure local rendering; no Git/network calls."""
from __future__ import annotations
import argparse
import csv
import json
import tempfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Circle, Rectangle
from matplotlib.colors import to_rgb
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
parser = argparse.ArgumentParser(description="Render the four figures from public result tables.")
parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/figures")
parser.add_argument("--png", action="store_true", help="Also export 3200-pixel PNGs")
ARGS = parser.parse_args()
OUT = ARGS.output_dir.resolve()
OUT.mkdir(parents=True, exist_ok=True)
ASSETS = ROOT / "docs/figures/logos"
DATA_ROOT = ROOT / "data/results"
SUMMARY = json.loads((DATA_ROOT / "summary.json").read_text())
MODELS = sorted(SUMMARY["models"], key=lambda m: -float(m["pokebench_score"]))
with (DATA_ROOT / "episodes.csv").open() as handle:
    EPISODES = list(csv.DictReader(handle))
for episode in EPISODES:
    episode["win"] = int(episode["win"])
    episode["opponent_hp_depletion"] = float(episode["opponent_hp_depletion"])
DATA = {"team_order": sorted({e["team_id"] for e in EPISODES})}
INK="#222222";MUTED="#666666";GRID="#E8E8E8";FAINT="#F3F3F3"
GREEN="#227B68";LOSS="#E9E9E9"
STYLES={
 "claude-opus-5-5":("Opus 5.5","claude","#BF765C","Claude Code · max"),
 "gemini-4-carbon-high":("Gemini 4 Carbon","gemini","#4569C9","Antigravity · high"),
 "gpt-6-astra":("GPT-6 Astra","openai","#267B68","Codex · max"),
 "gemini-4-neon-xhigh":("Gemini 4 Neon","gemini","#8564B6","Antigravity · xhigh"),
 "gpt-6.1-sol":("GPT-6.1 Sol","openai","#6A9C8E","Codex · max"),
 "deepseek-flash":("DeepSeek Flash","deepseek","#4D6BFE","OpenCode · max"),
}
plt.rcParams.update({"svg.fonttype":"path","svg.hashsalt":"pokebench-four-figures-v2",
 "font.family":"DejaVu Sans","axes.unicode_minus":False,"savefig.facecolor":"white"})
FONT={}
if Path("/System/Library/Fonts/Avenir Next.ttc").exists():
 from fontTools.ttLib import TTCollection
 collection=TTCollection("/System/Library/Fonts/Avenir Next.ttc")
 cache=Path(tempfile.gettempdir())/"pokebench-chart-fonts";cache.mkdir(exist_ok=True)
 for key,i in [("regular",7),("medium",5),("demi",2)]:
  path=cache/f"{key}.ttf"
  if not path.exists():collection.fonts[i].save(str(path))
  FONT[key]=FontProperties(fname=str(path))
else:
 for key,weight in [("regular","normal"),("medium","medium"),("demi","semibold")]:
  FONT[key]=FontProperties(family="DejaVu Sans",weight=weight)

LOGOS={"gemini":Image.open(ASSETS/"gemini-official.png").convert("RGBA")}
for brand,file in [("claude","claude-official.svg"),("openai","openai.svg"),("deepseek","deepseek.svg")]:
 import cairosvg,io
 raw=(ASSETS/file).read_text().replace("currentColor",INK)
 LOGOS[brand]=Image.open(io.BytesIO(cairosvg.svg2png(bytestring=raw.encode(),output_width=384,output_height=384))).convert("RGBA")

FIGURES=[]
class Figure:
 def __init__(self,height=920):
  self.w=1600;self.h=height
  # Tiny epsilon avoids float truncation of 9.2in × 200dpi to1839px.
  self.fig=plt.figure(figsize=(16,(height+1e-6)/100),dpi=100,facecolor="white")
  self.ax=self.fig.add_axes([0,0,1,1]);self.ax.set(xlim=(0,1600),ylim=(height,0));self.ax.axis("off")
  self.texts=[]
 def text(self,x,y,s,size=24,color=INK,weight="regular",ha="left",va="top",**kw):
  t=self.ax.text(x,y,s,fontsize=size*.72,fontproperties=FONT[weight],color=color,ha=ha,va=va,**kw)
  self.texts.append(t);return t
 def rect(self,x,y,w,h,color,**kw):
  r=Rectangle((x,y),w,h,fc=color,ec="none",**kw);self.ax.add_patch(r);return r
 def line(self,x1,y1,x2,y2,color=GRID,lw=1,**kw):
  return self.ax.plot([x1,x2],[y1,y2],color=color,lw=lw,**kw)
 def dot(self,x,y,r,color,**kw):
  p=Circle((x,y),r,fc=color,ec="none",**kw);self.ax.add_patch(p)
 def logo(self,provider,x,y,s=34):
  self.ax.imshow(np.asarray(LOGOS[provider]),extent=(x,x+s,y+s,y),aspect="auto",interpolation="lanczos",zorder=3)
  self.ax.set(xlim=(0,1600),ylim=(self.h,0))
 def model(self,model,x,y,sub=True,size=27):
  name,brand,color,harness=STYLES[model]
  self.logo(brand,x,y+4,35)
  self.text(x+51,y,name,size,weight="medium")
  if sub:self.text(x+51,y+38,harness,18,MUTED)
 def title(self,text):self.text(800,37,text,42,weight="medium",ha="center")
 def ticks(self,x,width,top,bottom,label_y,percent=False):
  for tick in [0,25,50,75,100]:
   xx=x+width*tick/100;self.line(xx,top,xx,bottom,GRID,.9,zorder=.4)
   self.text(xx,label_y,f"{tick}{'%' if percent else ''}",19,MUTED,ha="center")
 def save(self,name,title,alt):
  self.fig.canvas.draw();r=self.fig.canvas.get_renderer()
  for t in self.texts:
   e=t.get_window_extent(r)
   assert e.x0>=-1 and e.x1<=1601 and e.y0>=-1 and e.y1<=self.h+1,(name,t.get_text())
  if ARGS.png:
   self.fig.savefig(OUT/f"{name}.png",dpi=200,facecolor="white")
  self.fig.savefig(OUT/f"{name}.svg",facecolor="white",metadata={"Title":title,"Description":alt,"Date":"2026-11-20","Creator":"PokeBench"})
  plt.close(self.fig)
  FIGURES.append({"id":name,"title":title,"alt":alt,"width":1600,"height":self.h,"png":name+".png","svg":name+".svg"})

def pale(color,amount=.72):
 return tuple(c*(1-amount)+amount for c in to_rgb(color))

def master():
 f=Figure(920);f.title("PokéBench Score")
 x,width=500,880
 f.rect(933,123,22,18,"#777777");f.text(966,118,"Win credit",20,MUTED)
 f.rect(1139,123,22,18,"#D8D8D8");f.text(1172,118,"Loss HP credit",20,MUTED)
 f.text(1518,118,"Score",20,MUTED,ha="right")
 f.ticks(x,width,177,716,740)
 for i,m in enumerate(MODELS):
  y=207+i*84;model=m["model"];color=STYLES[model][2]
  f.model(model,64,y-8)
  w=float(m["win_credit"]);h=float(m["loss_hp_credit"])
  f.rect(x,y+5,width*w/100,31,color)
  f.rect(x+width*w/100,y+5,width*h/100,31,pale(color))
  f.text(1518,y+1,f"{float(m['pokebench_score']):.2f}",30,weight="demi",ha="right")
 f.text(x+width/2,782,"Score (0–100)",23,MUTED,ha="center")
 f.text(800,838,"Score = 0.5 × win rate + 0.5 × mean opponent HP depleted",23,ha="center")
 f.text(800,878,"100 points per win; half the HP-depletion percentage per loss. Mean of 30 battles.",19,MUTED,ha="center")
 f.save("pokebench-score","PokéBench Score",
  "PokéBench Score v1 is the equal average of win rate and mean terminal opponent HP depletion, both in percent. Equivalently, each win earns100points and each loss earns half its HP-depletion percentage; average the same30battles. Opus78.12, Carbon70.57, Astra65.90, Neon65.74, Sol64.21, Flash43.76. Dark bar segments show win credit; light segments show partial loss HP credit. Fixed0–100scale. The formula was defined after these runs; methodology documents weights and limitations.")

def performance():
 f=Figure(920);f.title("Model Performance")
 xw,ww=500,400;xh,wh=1070,345
 f.text(xw,131,"Win rate (%)",24,weight="medium")
 f.text(xh,131,"Opponent HP depleted (%)",24,weight="medium")
 for x,w in [(xw,ww),(xh,wh)]:f.ticks(x,w,184,725,751)
 for i,m in enumerate(MODELS):
  y=213+i*84;color=STYLES[m["model"]][2]
  f.model(m["model"],64,y-10)
  w=float(m["win_rate_percent"]);h=float(m["mean_opponent_hp_depletion"])
  f.rect(xw,y+4,ww*w/100,27,color);f.rect(xh,y+4,wh*h/100,27,color)
  f.text(943,y+1,f"{m['wins']}/30",26,weight="medium")
  f.text(xw+ww*w/100,y+41,f"{w:.1f}%",19,color,ha="right")
  f.text(1518,y+1,f"{h:.2f}%",26,weight="medium",ha="right")
 f.text(800,840,"30 battles per model. HP depletion is measured at battle end and averaged over Cynthia’s six Pokémon.",20,MUTED,ha="center")
 f.text(800,878,"Model and harness configurations are listed in the methodology.",18,MUTED,ha="center")
 f.save("pokebench-results","Model Performance",
  "Win rate and mean terminal opponent HP depletion for the same30battles per native system. Opus20/30and89.58%HP; Carbon17/30and84.47%; Astra15/30and81.80%; Neon15/30and81.49%; Sol14/30and81.76%; Flash5/30and70.86%. HP depletion weights each of Cynthia's six original Pokémon equally. Raw component metrics are retained separately from the composite score.")

def battle_grid():
 f=Figure(830);f.title("Battle Results")
 gx,cell,gap,group=493,22,5,90
 f.rect(1122,119,20,18,GREEN);f.text(1154,114,"Win",20,MUTED)
 f.rect(1237,119,20,18,LOSS);f.text(1269,114,"Loss",20,MUTED)
 f.text(1518,114,"Wins",20,MUTED,ha="right")
 for i,team in enumerate(DATA["team_order"]):
  x=gx+i*group;f.text(x+38,179,team.replace("hard-",""),23,ha="center")
  for j in range(3):f.text(x+j*(cell+gap)+cell/2,216,str(j+1),16,MUTED,ha="center")
  if i>0:f.line(x-10,177,x-10,681,GRID,.8)
 lookup={(e["model"],e["case"]):e for e in EPISODES}
 for i,m in enumerate(MODELS):
  y=273+i*70;f.model(m["model"],64,y-5,sub=False)
  for t,team in enumerate(DATA["team_order"]):
   for j in range(3):
    e=lookup[m["model"],f"{team}-seed{j+1}"]
    f.rect(gx+t*group+j*(cell+gap),y,cell,30,GREEN if e["win"] else LOSS)
  f.text(1518,y-1,f"{m['wins']}/30",27,weight="medium",ha="right")
 f.text(gx+443,712,"Team / starting RNG state",22,MUTED,ha="center")
 f.text(800,777,"10 teams × 3 starting states per model. Cells show genuine game outcomes.",20,MUTED,ha="center")
 f.save("pokebench-fixtures","Battle Results",
  "All180genuine game outcomes, six models by30matched starting fixtures, grouped into10teams with three starting RNG states each. Solid green rectangles are wins; light gray rectangles are losses. Win totals20,17,15,15,14,5 in composite-score order.")

def hp_distribution():
 f=Figure(920);f.title("Opponent HP Depleted")
 x,width=500,880
 f.dot(1000,127,5,MUTED);f.text(1016,114,"Battle",20,MUTED)
 f.rect(1137,121,12,12,INK);f.text(1162,114,"Mean",20,MUTED)
 f.text(1518,114,"Mean",20,MUTED,ha="right")
 f.ticks(x,width,177,725,751,percent=True)
 for i,m in enumerate(MODELS):
  y=236+i*80;model=m["model"];color=STYLES[model][2]
  f.model(model,64,y-16,sub=False)
  f.line(x,y,x+width,y,GRID,.7,zorder=.4)
  rng=np.random.default_rng(1940+i)
  games=sorted([e for e in EPISODES if e["model"]==model],key=lambda e:(e["opponent_hp_depletion"],e["case"]))
  for e in games:
   f.dot(x+width*e["opponent_hp_depletion"]/100,y+float(rng.uniform(-23,23)),4.4,color,alpha=.7)
  h=float(m["mean_opponent_hp_depletion"])
  f.rect(x+width*h/100-6,y-6,12,12,INK,zorder=5)
  f.text(1518,y-16,f"{h:.2f}%",28,weight="medium",ha="right")
 f.text(x+width/2,795,"Opponent HP depleted (%)",23,MUTED,ha="center")
 f.text(800,853,"Terminal HP depletion, equally weighted over six opponents. 30 battles per model; wins have 100% depletion.",19,MUTED,ha="center")
 f.text(800,885,"Dots are vertically jittered and may overlap.",18,MUTED,ha="center")
 f.save("pokebench-progress","Opponent HP Depleted",
  "Distribution of all30terminal HP-depletion scores per model; dots are battles and black squares are means. Opus89.58%, Carbon84.47%, Astra81.80%, Neon81.49%, Sol81.76%, Flash70.86%. Each original opponent contributes equally. Healing can reduce depletion. Wins have100%depletion. Vertical jitter does not change exact horizontal scores. These are final states rather than a time-series.")

if __name__ == "__main__":
    master()
    performance()
    hp_distribution()
    battle_grid()
    print(f"Rendered {len(FIGURES)} figures in {OUT}")
