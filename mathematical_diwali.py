"""Happy Diwali — a mathematical animation made with Python.

The letters are traced with truncated Fourier series:
    z(u) = sum_k c_k * exp(2*pi*i*k*u).
Firework particles follow polar rays plus gravity; the decorative rose curve
follows r(theta) = a + b*cos(8*theta + phase).

Install: pip install numpy pillow opencv-python
Run:     python mathematical_diwali.py
Optional: install ffmpeg (for an MP4 and an optimized GIF).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 960, 540
FPS = 24
DURATION = 7.5
N_FRAMES = int(FPS * DURATION)
OUT = Path(__file__).resolve().parent
RNG = np.random.default_rng(2026)


def font(size: int, bold: bool = False):
    candidates = ([
        '/usr/share/fonts/truetype/lato/Lato-Black.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        'C:/Windows/Fonts/arialbd.ttf',
    ] if bold else [
        '/usr/share/fonts/truetype/lato/Lato-Regular.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        'C:/Windows/Fonts/arial.ttf',
    ])
    for f in candidates:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


FONT_TITLE = font(90, bold=True)
FONT_MAIN = font(125, bold=True)
FONT_CAPTION = font(20)
FONT_FOOT = font(20)
FONT_MATH = font(16)


def track_draw(draw, center_x, y, s, f, fill, tracking=5):
    widths = [draw.textbbox((0, 0), c, font=f)[2] for c in s]
    width = sum(widths) + tracking * (len(s) - 1)
    x = center_x - width / 2
    for c, cw in zip(s, widths):
        draw.text((round(x), y), c, font=f, fill=fill)
        x += cw + tracking


def background():
    yy, xx = np.mgrid[0:H, 0:W]
    rr = ((xx-W/2)/(W*.77))**2 + ((yy-H*.56)/(H*.92))**2
    glow = np.exp(-rr*2.3)
    top = np.clip(1-yy/H, 0, 1)
    arr = np.zeros((H,W,3),dtype=np.uint8)
    arr[:,:,0] = 7 + 20*glow + 6*(1-top)
    arr[:,:,1] = 8 + 6*glow
    arr[:,:,2] = 30 + 31*glow + 5*top
    im = Image.fromarray(arr, 'RGB').convert('RGBA')
    d = ImageDraw.Draw(im, 'RGBA')
    # Thin frame and precise corner decorations.
    d.rounded_rectangle((29, 24, W-30, H-11), radius=22,
                        outline=(234,166,66,64), width=2)
    for x, direction in [(44,1),(W-45,-1)]:
        for radius in (22,32,44):
            d.arc((x-radius,45-radius,x+radius,45+radius),
                  start=15 if direction==1 else 100,
                  end=85 if direction==1 else 170,
                  fill=(232,162,76,100), width=2)
    return im


BG = background()
STARS = np.column_stack((RNG.uniform(45,W-45,92), RNG.uniform(37,H-52,92),
                         RNG.uniform(1.0,2.4,92), RNG.uniform(0,2*math.pi,92)))


def letter_shapes(text, typeface, y, spacing, start, step, seconds):
    widths = [ImageDraw.Draw(Image.new('L',(1,1))).textlength(c,font=typeface) for c in text]
    total = sum(widths) + spacing*(len(text)-1)
    cursor = (W-total)/2
    letters = []
    for idx,(ch,width) in enumerate(zip(text,widths)):
        mask = Image.new('L',(W,H),0)
        d = ImageDraw.Draw(mask)
        # Anchor at top bearing; Pillow handles font metrics consistently.
        d.text((int(cursor),y),ch,font=typeface,fill=255)
        array = np.asarray(mask)
        contours,_ = cv2.findContours(array, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        contour = max(contours,key=cv2.contourArea)[:,0,:].astype(np.float64)
        # Uniformly sample the closed contour by arc length.
        cyc = np.concatenate([contour,contour[:1]],axis=0)
        seg = np.sqrt(np.sum(np.diff(cyc,axis=0)**2,axis=1))
        arc = np.concatenate([[0],np.cumsum(seg)])
        u = np.linspace(0,arc[-1],700,endpoint=False)
        z = np.interp(u,arc,cyc[:,0])+1j*np.interp(u,arc,cyc[:,1])
        coeffs = np.fft.fft(z)/len(z)
        freqs = np.fft.fftfreq(len(z),d=1/len(z))
        # Use the 101 lowest Fourier frequencies to draw smooth outlines.
        keep = np.abs(freqs)<=50
        phase = np.linspace(0,1,550,endpoint=False)
        path = ((coeffs[keep,None] *
                 np.exp(2j*np.pi*freqs[keep,None]*phase[None,:])).sum(axis=0))
        traced = [(float(a.real),float(a.imag)) for a in path]
        letters.append({'mask':mask,'trace':traced,'start':start+idx*step,
                        'end':start+idx*step+seconds})
        cursor += width + spacing
    return letters


LETTERS = (letter_shapes('HAPPY',FONT_TITLE,112,14,0.55,0.36,0.34) +
           letter_shapes('DIWALI',FONT_MAIN,226,8,2.43,0.43,0.40))

# Blur each finished glyph just once; composite it cheaply per frame.
for letter in LETTERS:
    letter['halo'] = letter['mask'].filter(ImageFilter.GaussianBlur(12))
    letter['soft'] = letter['mask'].filter(ImageFilter.GaussianBlur(3))


def lerp(a,b,t):
    return a + (b-a)*t


def fireworks(draw, t):
    bursts = [
        (154,153,0.20, (252,165,75),34,94),
        (800,149,1.12, (152,138,255),35,94),
        (114,316,2.36, (245,105,158),21,59),
        (853,308,3.27, (255,203,87),22,67),
        (170,142,4.75, (105,211,244),31,97),
        (804,147,5.45, (255,173,96),31,100),
        (115,297,6.65, (245,106,169),20,62),
    ]
    for cx,cy,start,col,n,speed in bursts:
        age = t-start
        if not (0 <= age <= 1.5):
            continue
        fade = (max(0,1-age/1.5))**1.2
        for k in range(n):
            th=2*np.pi*k/n + (k%3)*.075
            vel = speed*(.8 + .3*((k*13)%19)/19)
            x = cx + vel*age*np.cos(th)
            y = cy + vel*age*np.sin(th) + 24*age**2
            px = cx + vel*max(0,age-.11)*np.cos(th)
            py = cy + vel*max(0,age-.11)*np.sin(th) + 24*max(0,age-.11)**2
            alpha = int(200*fade)
            draw.line((px,py,x,y),fill=(*col,alpha),width=2)
            r=1.2+1.5*fade
            draw.ellipse((x-r,y-r,x+r,y+r),fill=(*col,min(255,int(235*fade))))


def rose_curve(draw,t):
    for cx, cy, scale in ((170,348,0.73),(790,348,0.73)):
        th = np.linspace(0,2*np.pi,320)
        r = scale*(58 + 25*np.cos(8*th+0.16*t))
        xs = cx+r*np.cos(th)
        ys = cy+r*np.sin(th)
        points = list(zip(xs,ys))
        draw.line(points+[points[0]],fill=(225,152,83,52),width=1)
        r2=scale*34
        draw.ellipse((cx-r2,cy-r2,cx+r2,cy+r2),outline=(242,162,68,48),width=1)


def diya(draw,t):
    cx,cy=480,408
    # Tiny radial ornaments around the diya, eightfold symmetric.
    for j in range(16):
        th=j*2*math.pi/16 + t*0.07
        r=103+7*math.sin(8*th+t*0.7)
        x,y=cx+r*math.cos(th),cy-7+r*.46*math.sin(th)
        draw.ellipse((x-2,y-2,x+2,y+2),fill=(243,174,69,110))

    # The flame is a time-varying tapered sinusoidal envelope.
    sway = 4.5*math.sin(2*math.pi*.65*t)
    s = np.linspace(0,1,51)
    center = cx + sway*s**1.4 + 7*np.sin(np.pi*s)
    width = 19*(1-s)**1.07 * (0.92+0.08*np.sin(8*t))
    fy = 391-44*s
    outer = list(zip(center-width,fy)) + list(zip((center+width)[::-1],fy[::-1]))
    draw.polygon(outer,fill=(255,140,28,210))
    center2 = cx + sway*s**1.4 + 5*np.sin(np.pi*s)
    width2 = 10*(1-s)**1.15
    fy2=392-33*s
    inner = list(zip(center2-width2,fy2))+list(zip((center2+width2)[::-1],fy2[::-1]))
    draw.polygon(inner,fill=(255,228,111,238))

    # Bowl with a mathematically defined quadratic edge.
    x = np.linspace(-82,82,100)
    upper = [(cx+xi,398+4*(xi/82)**2) for xi in x]
    lower = [(cx+xi,430-28*(xi/82)**2) for xi in x[::-1]]
    draw.polygon(upper+lower,fill=(210,84,34,255))
    draw.arc((cx-82,382,cx+82,418),0,180,fill=(255,191,70,255),width=5)
    draw.line([(cx-72,413),(cx-40,421),(cx,428),(cx+40,421),(cx+72,413)],
              fill=(252,176,70,164),width=3)
    for j in range(-3,4):
        draw.ellipse((cx+j*21-2,415,cx+j*21+2,419),fill=(255,205,89,225))


def draw_letters(img,t):
    completed = Image.new('L',(W,H),0)
    active=[]
    for ch in LETTERS:
        if t>=ch['end']:
            completed = Image.fromarray(np.maximum(np.asarray(completed),np.asarray(ch['mask'])))
        elif t>=ch['start']:
            active.append((ch,(t-ch['start'])/(ch['end']-ch['start'])))

    glow = completed.filter(ImageFilter.GaussianBlur(10))
    warm = Image.new('RGBA',(W,H),(248,143,49,0))
    warm.putalpha(glow.point(lambda x: int(x*.53)))
    img.alpha_composite(warm)
    sharp=Image.new('RGBA',(W,H),(255,204,91,0))
    sharp.putalpha(completed)
    img.alpha_composite(sharp)
    # Bright highlight at the top of every letter.
    edge = completed.filter(ImageFilter.MaxFilter(3))
    halo = Image.new('RGBA',(W,H),(255,235,164,0))
    halo.putalpha(Image.fromarray(np.clip(np.asarray(edge,dtype=np.int16)-np.asarray(completed,dtype=np.int16),0,255).astype('uint8')))
    img.alpha_composite(halo)

    if active:
        trails=Image.new('RGBA',(W,H),(0,0,0,0))
        dt=ImageDraw.Draw(trails,'RGBA')
        for ch,progress in active:
            pts=ch['trace']
            m=max(2,int(len(pts)*max(0,progress)))
            dt.line(pts[:m],fill=(255,189,82,236),width=3,joint='curve')
            x,y=pts[min(m-1,len(pts)-1)]
            for r,a in ((15,29),(8,60),(4,230)):
                dt.ellipse((x-r,y-r,x+r,y+r),fill=(255,225,123,a))
        img.alpha_composite(trails.filter(ImageFilter.GaussianBlur(5)))
        img.alpha_composite(trails)


def render(t):
    im=BG.copy()
    d=ImageDraw.Draw(im,'RGBA')
    # Deterministic flicker: sinusoids with star-specific phases.
    for x,y,r,phase in STARS:
        a = int(46+55*(.5+.5*math.sin(2.6*t+phase)))
        if not (92<y<335 and 260<x<700):
            d.ellipse((x-r,y-r,x+r,y+r),fill=(255,219,162,a))
    rose_curve(d,t)
    fireworks(d,t)
    track_draw(d,W//2,60,'A CELEBRATION OF LIGHT',FONT_CAPTION,(255,198,126,188),tracking=4)
    d.line((342,94,618,94),fill=(246,180,80,75),width=2)

    draw_letters(im,t)
    # A soft halo under the flame.
    flame=Image.new('RGBA',(W,H),(0,0,0,0))
    hf=ImageDraw.Draw(flame,'RGBA')
    hf.ellipse((438,315,532,425),fill=(255,110,29,97))
    im.alpha_composite(flame.filter(ImageFilter.GaussianBlur(27)))
    d=ImageDraw.Draw(im,'RGBA')
    diya(d,t)
    track_draw(d,W//2,469,'MAY YOUR LIFE SHINE BRIGHT',FONT_FOOT,(255,214,155,211),tracking=3)
    d.text((56,502),'z(u) = sum c_k exp(2*pi*i*k*u)',font=FONT_MATH,fill=(226,165,105,130))
    d.text((664,502),'r(theta) = a + b cos(8*theta)',font=FONT_MATH,fill=(226,165,105,130))
    return im.convert('RGB')


def main():
    mp4 = OUT/'happy_diwali_mathematical.mp4'
    gif = OUT/'happy_diwali_mathematical.gif'
    ffmpeg=shutil.which('ffmpeg')
    if ffmpeg:
        proc=subprocess.Popen([ffmpeg,'-y','-loglevel','error','-f','rawvideo',
            '-vcodec','rawvideo','-pix_fmt','rgb24','-s',f'{W}x{H}',
            '-r',str(FPS),'-i','-','-an','-c:v','libx264','-preset','medium',
            '-crf','19','-pix_fmt','yuv420p','-movflags','+faststart',str(mp4)],
            stdin=subprocess.PIPE)
        for i in range(N_FRAMES):
            t=i/FPS
            proc.stdin.write(render(t).tobytes())
        proc.stdin.close()
        if proc.wait()!=0:
            raise RuntimeError('ffmpeg failed to create MP4')
        # Smaller shareable GIF, palette generated from the video.
        pal=OUT/'_diwali_palette.png'
        subprocess.run([ffmpeg,'-y','-loglevel','error','-i',str(mp4),
            '-vf','fps=15,scale=768:-1:flags=lanczos,palettegen=stats_mode=diff',str(pal)],check=True)
        subprocess.run([ffmpeg,'-y','-loglevel','error','-i',str(mp4),
            '-i',str(pal),'-lavfi',
            '[0:v]fps=15,scale=768:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4',
            '-loop','0',str(gif)],check=True)
        pal.unlink(missing_ok=True)
    else:
        frames = [render(i/FPS).resize((768,432)) for i in range(N_FRAMES)]
        frames[0].save(gif,save_all=True,append_images=frames[1:],
                       duration=round(1000/FPS),loop=0,optimize=True)
    print('Created:', gif)
    if mp4.exists():
        print('Created:',mp4)


if __name__=='__main__':
    main()
