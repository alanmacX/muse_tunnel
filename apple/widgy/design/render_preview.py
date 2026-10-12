"""Deterministic 173pt UI design preview, not a native Widgy screenshot."""
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).parent
S=3
fontfile='/System/Library/Fonts/STHeiti Medium.ttc'
def font(size):return ImageFont.truetype(fontfile,round(size*S))
def preview(theme='dark',state='normal'):
    palette={'dark':('#202420','#f3f3eb','#afb5a9','#e1b66a','#363d35'),
             'light':('#f5f4ee','#29332b','#758173','#8b6331','#dfE3d8')}[theme]
    bg,fg,muted,accent,line=palette
    im=Image.new('RGB',(173*S,173*S),bg);d=ImageDraw.Draw(im)
    def txt(x,y,value,size,color=fg):d.text((x*S,y*S),value,font=font(size),fill=color)
    def rule(y):d.line((14*S,y*S,159*S,y*S),fill=line,width=S)
    d.ellipse((14*S,16*S,19*S,21*S),fill=accent)
    txt(25,12,'MUSE',10,fg);txt(129,13,'10.12',8,muted)
    if state=='empty':
        txt(14,39,'今日课程已结束',13);txt(14,59,'或无课',13)
    elif state=='unknown':
        txt(14,39,'课程安排',13);txt(14,59,'等待 Muse 补齐',13)
    else:
        txt(14,39,'进行中',11,accent);txt(68,37,'广B204',14)
        txt(14,60,'13:30',12);txt(68,59,'博易C103',13)
    rule(84)
    if state=='unknown':
        txt(14,97,'等待同步重要事项',11,muted)
    else:
        for y,text in ((96,'核对明天实验安排'),(119,'准备社会实践报告')):
            d.ellipse((14*S,(y+4)*S,18*S,(y+8)*S),fill=accent)
            txt(24,y,text,11)
    txt(14,150,'过期 · 10:00' if state=='stale' else '核对 10:53' if state!='unknown' else '尚未维护',8,accent if state=='stale' else muted)
    # Round clipped corners, like a small home-screen widget.
    mask=Image.new('L',im.size,0);ImageDraw.Draw(mask).rounded_rectangle((0,0,173*S-1,173*S-1),radius=24*S,fill=255)
    backdrop=Image.new('RGB',im.size,'#eceee8');backdrop.paste(im,(0,0),mask)
    return backdrop
if __name__=='__main__':
    items=[('dark','normal'),('light','normal'),('dark','stale'),('light','empty'),('dark','unknown')]
    w=(173+24)*S;canvas=Image.new('RGB',(w*len(items)+24*S,235*S),'#eceee8');d=ImageDraw.Draw(canvas)
    for i,(theme,state) in enumerate(items):
        x=(24+i*(173+24))*S
        canvas.paste(preview(theme,state),(x,35*S))
        d.text((x,15*S),{'normal':'日常 · '+theme,'stale':'过期提示','empty':'课程已结束','unknown':'等待补齐'}[state],font=font(9),fill='#596351')
    d.text((24*S,217*S),'小号 173pt · 设计预览 / 示例数据，非 Widgy 实机截图',font=font(9),fill='#596351')
    canvas.save(ROOT/'muse-small-preview.png')
    preview().save(ROOT/'muse-small-dark.png')
    print('Rendered 5 design states at 173pt, 3x density.')
