import json, sys
sys.path.insert(0, "/home/aaron/playerone")
import cv2
from pathlib import Path
from ultralytics import YOLO
P1 = Path("/home/aaron/playerone")
EVID = P1/"evidence/perception/star-visitor/frames"
gt = json.loads((P1/"yolo/eval_gt_star-visitor.json").read_text())["frames"]
def iou(a,b):
    ax1,ay1,ax2,ay2=a[0],a[1],a[0]+a[2],a[1]+a[3]
    bx1,by1,bx2,by2=b[0],b[1],b[0]+b[2],b[1]+b[3]
    iw=min(ax2,bx2)-max(ax1,bx1); ih=min(ay2,by2)-max(ay1,by1)
    if iw<=0 or ih<=0: return 0.0
    i=iw*ih
    return i/float(a[2]*a[3]+b[2]*b[3]-i)
m = YOLO("/home/aaron/playerone/yolo/best.pt")
for conf in (0.25, 0.15, 0.10):
    fp = {}; tot = 0
    for p in sorted(EVID.glob("*.jpg")):
        f = cv2.imread(str(p))
        gtl = [(c,x,y,w,h) for c,x,y,w,h in gt.get(p.stem[-3:],[])]
        r = m.predict(f, imgsz=800, conf=conf, verbose=False)[0]
        boxes=[]
        for b in r.boxes:
            n = r.names[int(b.cls)]
            if not n.startswith("star-visitor."): continue
            x0,y0,x1,y1 = (float(v) for v in b.xyxy[0])
            boxes.append((n.split(".",1)[1], x0,y0,x1-x0,y1-y0, float(b.conf)))
        # crude NMS like perception v2 (0.35 same-class)
        boxes.sort(key=lambda d:-d[5])
        keep=[]
        for d in boxes:
            if any(d[0]==k[0] and iou(d[1:5],k[1:5])>0.35 for k in keep): continue
            keep.append(d)
        for c,x,y,w,h,cf in keep:
            tot += 1
            if not any(c==gc and iou((x,y,w,h),(gx,gy,gw,gh))>0.4 for gc,gx,gy,gw,gh in gtl):
                fp[c] = fp.get(c,0)+1
    print(f"conf={conf}: kept={tot} FP={sum(fp.values())} {fp}")
