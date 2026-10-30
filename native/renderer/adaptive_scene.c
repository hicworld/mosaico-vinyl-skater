/* Portable production renderer: shared vinyl, transparent reusable character
 * clips, and continuously advected skate marks from the visible wheel contacts. */
#include "adaptive_scene.h"
#include "adaptive_art.h"
#include "nature_art.h"
#include "scene_ambience.h"
#include "record_colors.h"
#include "art_codec.h"
#include "neon_character.h"
#include "foot_effects.h"
#include <math.h>
#include <string.h>
#include <limits.h>
#include <stdlib.h>
#ifdef ESP_PLATFORM
#include "esp_timer.h"
#include "esp_attr.h"
#define SCENE_IRAM_ATTR IRAM_ATTR
static uint32_t scene_now_us(void){return (uint32_t)esp_timer_get_time();}
#else
#define SCENE_IRAM_ATTR
static uint32_t scene_now_us(void){return 0;}
#endif
#define PI 3.14159265358979323846f
_Static_assert(ADAPTIVE_CLIPS == SONG_CLIP_COUNT, "motion and sprite clip counts must match");
_Static_assert(ADAPTIVE_THEMES + NATURE_SCENES == SCENE_THEMES, "scene and art theme counts must match");
_Static_assert(SCENE_PALETTE_COUNT == SONG_THEME_COUNT, "Scene palettes must cover every scene");
_Static_assert(RECORD_COLOR_COUNT == SCENE_RECORD_COLOR_COUNT, "record runtime colour counts must match");
_Static_assert(SCENE_SCENE_RECORD_BASE == SONG_RECORD_COLOR_COUNT, "standard record metadata count");
_Static_assert(SCENE_SCENE_RECORD_BASE+SCENE_SCENE_RECORD_COUNT == SCENE_RECORD_COLOR_COUNT, "scene record palette count");
_Static_assert(SCENE_SKINS == ADAPTIVE_SKINS + 1, "Neon follows the three shared sprite palettes");
_Static_assert(SCENE_SCRATCH_BYTES >= NEON_SCRATCH_BYTES, "Neon mesh workspace must fit the scene scratch");
_Static_assert(SCENE_MAIN_SCRATCH_BYTES >= ADAPTIVE_MAX_RAW_BYTES, "Assets must fit the fixed main workspace");
_Static_assert(SCENE_MAIN_SCRATCH_BYTES%128u==0 && SCENE_WORKER_SCRATCH_BYTES%128u==0, "Native scratch partitions must be aligned");
_Static_assert(SCENE_EFFECTS == FOOT_EFFECT_COUNT, "Effect IDs must match the song package contract");
static float clampf(float x,float a,float b){return x<a?a:(x>b?b:x);}
static float smoothf(float x){x=clampf(x,0,1);return x*x*(3-2*x);}
static void project(float u,float v,float z,float *x,float *y){
    const float angle=-23.37f*PI/180,ca=cosf(angle),sa=sinf(angle);
    *x=195.56f+(ca*u-sa*.738f*v)*(4.0f/3);
    *y=303.36f+(sa*u+ca*.738f*v)*(16.0f/15)-z*(16.0f/15);
}
static bool decode(adaptive_scene_t *s,const adaptive_asset_t *a){
    return a->offset<=sizeof(adaptive_lz4)&&a->packed<=sizeof(adaptive_lz4)-a->offset
        &&a->raw<=ADAPTIVE_MAX_RAW_BYTES&&a->raw<=s->main_capacity&&art_decode(adaptive_lz4+a->offset,a->packed,s->scratch,a->raw);
}
static unsigned canonical_theme(unsigned theme){return theme?theme:1;}
static unsigned art_theme(unsigned theme){
    return theme>ADAPTIVE_THEMES?nature_base_themes[theme-ADAPTIVE_THEMES-1]:theme;
}
static bool decode_nature(adaptive_scene_t *s,unsigned offset,unsigned packed,unsigned raw){
    return offset<=sizeof(nature_lz4)&&packed<=sizeof(nature_lz4)-offset
        &&raw<=s->main_capacity&&art_decode(nature_lz4+offset,packed,s->scratch,raw);
}
static unsigned expand_channel(unsigned value,unsigned max){return value*255/max;}
static void paper_init(adaptive_scene_t *s,unsigned theme){
    unsigned base=art_theme(theme);
    float paper[3],direction[3],denominator=0;
    for(unsigned j=0;j<3;j++){
        unsigned bits=j==1?63:31,shift=j==1?2:3;
        paper[j]=(float)expand_channel(scene_source_paper[base][j]>>shift,bits);
        float ink=(float)expand_channel(scene_source_ink[base][j]>>shift,bits);
        direction[j]=ink-paper[j];denominator+=direction[j]*direction[j];
    }
    float bias=0;
    for(unsigned j=0;j<3;j++){
        unsigned levels=j==1?64:32;
        int16_t *table=j==0?s->paper_red:(j==1?s->paper_green:s->paper_blue);
        float weight=255*direction[j]/denominator;
        bias-=paper[j]*weight;
        for(unsigned value=0;value<levels;value++)
            table[value]=(int16_t)lroundf(expand_channel(value,levels-1)*weight);
    }
    s->paper_bias=(int)lroundf(bias);
    for(unsigned i=0;i<256;i++){
        unsigned rgb[3];
        for(unsigned j=0;j<3;j++){
            int a=scene_paper_rgb[theme][j],b=scene_ink_rgb[theme][j];
            rgb[j]=(unsigned)(a+(b-a)*(int)i/255);
        }
        s->paper_ramp[i]=(uint16_t)(((rgb[0]>>3)<<11)|((rgb[1]>>2)<<5)|(rgb[2]>>3));
    }
}
static uint16_t paper_colour(const adaptive_scene_t *s,uint16_t colour){
    int shade=s->paper_bias+s->paper_red[(colour>>11)&31]
        +s->paper_green[(colour>>5)&63]+s->paper_blue[colour&31];
    return s->paper_ramp[shade<0?0:(shade>255?255:shade)];
}
static bool spindle(unsigned x,unsigned y){
    return x>=SCENE_SPINDLE_X&&x<SCENE_SPINDLE_X+SCENE_SPINDLE_W
        &&y>=SCENE_SPINDLE_Y&&y<SCENE_SPINDLE_Y+SCENE_SPINDLE_H
        &&(scene_spindle_mask[y-SCENE_SPINDLE_Y]&(1u<<(x-SCENE_SPINDLE_X)));
}
static void copy_environment(adaptive_scene_t *s,const uint16_t *source,unsigned source_stride,
                             uint16_t *destination,unsigned destination_stride){
    if(s->environment_copy&&s->environment_copy(s->environment_copy_context,source,source_stride,
                                               destination,destination_stride))return;
    for(unsigned row=0;row<480;row++)
        memcpy(destination+row*destination_stride,source+row*source_stride,960);
}
static bool background_theme(adaptive_scene_t *s,unsigned theme,unsigned record_id){
    theme=canonical_theme(theme);
    if(theme>=SONG_THEME_COUNT||record_id>=RECORD_COLOR_COUNT)return false;
    unsigned slot=art_theme(theme)-1;
    for(unsigned i=0;i<ADAPTIVE_TILES;i++){
        const adaptive_asset_t *a=&adaptive_static_background[slot*ADAPTIVE_TILES+i];
        if(a->raw!=2048||!decode(s,a))return false;
        for(unsigned row=0;row<32;row++)
            memcpy(s->background+(a->y+row)*480+a->x,s->scratch+row*64,64);
    }
    /* A nature scene stores only its outer scenery. The pressed material,
     * actor atlas and rotating paper remain shared with the original scenes. */
    if(theme>ADAPTIVE_THEMES)for(unsigned i=0;i<ADAPTIVE_TILES;i++){
        const nature_delta_t *delta=&nature_background[theme-ADAPTIVE_THEMES-1][i];
        if(!delta->packed)continue;
        if(!decode_nature(s,delta->offset,delta->packed,2048))return false;
        unsigned x=i%15*32,y=i/15*32;
        const uint16_t *pixels=(const uint16_t*)s->scratch;
        for(unsigned row=0;row<32;row++)for(unsigned col=0;col<32;col++){
            uint16_t colour=pixels[row*32+col];
            if(colour)s->background[(y+row)*480+x+col]=colour;
        }
    }
    paper_init(s,theme);
    const adaptive_asset_t *paper=&adaptive_static_label[slot*ADAPTIVE_LABELS];
    for(unsigned row=0;row<paper->h;row++)for(unsigned col=0;col<paper->w;col++){
        unsigned i=row*paper->w+col;
        if(adaptive_label_mask[i>>3]&(1u<<(i&7))){
            uint16_t *p=s->background+(paper->y+row)*480+paper->x+col;
            *p=paper_colour(s,*p);
        }
    }
    /* Tint only the vinyl material. Paper, rim, shadow and surroundings stay
     * byte exact; the luminance LUT preserves pressed grooves and highlights. */
    if(record_id)for(unsigned y=0;y<480;y++)for(unsigned part=0;part<2;part++){
        unsigned x0=record_body_spans[y][part*2],x1=record_body_spans[y][part*2+1];
        for(unsigned x=x0;x<x1;x++){
            uint16_t *pixel=s->background+y*480+x,colour=*pixel;
            unsigned r=((colour>>11)&31)*255/31,g=((colour>>5)&63)*255/63,b=(colour&31)*255/31;
            *pixel=record_colour_lut[record_id][(54*r+183*g+19*b)>>8];
        }
    }
    copy_environment(s,s->background,480,s->canvas,s->stride);
    s->profile.restore_tiles+=SCENE_TILES;
    memset(s->occupied,0,sizeof(s->occupied));
    memset(s->prepared,1,sizeof(s->prepared));
    memset(s->dirty,1,sizeof(s->dirty));
    s->last_theme=theme;
    s->last_record_id=record_id;
    return true;
}
#define RGB565(r,g,b) (((r>>3)<<11)|((g>>2)<<5)|(b>>3))
static uint16_t trail_colour(unsigned theme){
    static const uint16_t colours[SONG_THEME_COUNT]={
        RGB565(244,143,164),RGB565(255,202,117),
        RGB565(93,227,226),RGB565(223,125,88),
        RGB565(152,222,255),RGB565(188,246,150),RGB565(160,181,255)};
    return colours[theme];
}
static void restore(adaptive_scene_t *s,unsigned tile){
    unsigned x=tile%15*32,y=tile/15*32;
    for(unsigned r=0;r<32;r++)memcpy(s->canvas+(y+r)*s->stride+x,s->background+(y+r)*480+x,64);
    s->dirty[tile]=1;s->prepared[tile]=1;
}
static void label_opaque_init(adaptive_scene_t *s){
    s->label_opaque_valid=false;
    if(ADAPTIVE_LABEL_W>UINT8_MAX||ADAPTIVE_LABEL_H>sizeof(s->label_opaque_span)/sizeof(s->label_opaque_span[0])
       ||ADAPTIVE_LABEL_X+ADAPTIVE_LABEL_W>SCENE_WIDTH||ADAPTIVE_LABEL_Y+ADAPTIVE_LABEL_H>SCENE_HEIGHT)return;
    for(unsigned i=0;i<ADAPTIVE_THEMES*ADAPTIVE_LABELS;i++){
        const adaptive_asset_t *a=&adaptive_static_label[i];
        if(a->x!=ADAPTIVE_LABEL_X||a->y!=ADAPTIVE_LABEL_Y||a->w!=ADAPTIVE_LABEL_W||a->h!=ADAPTIVE_LABEL_H)return;
    }
    for(unsigned row=0;row<ADAPTIVE_LABEL_H;row++){
        unsigned left=ADAPTIVE_LABEL_W,right=0;bool ended=false;
        for(unsigned col=0;col<ADAPTIVE_LABEL_W;col++){
            unsigned i=row*ADAPTIVE_LABEL_W+col;
            if(adaptive_label_mask[i>>3]&(1u<<(i&7))){
                if(ended)return; /* A future mask with holes uses full copies. */
                if(left==ADAPTIVE_LABEL_W)left=col;
                right=col+1;
            }else if(right)ended=true;
        }
        if(left==ADAPTIVE_LABEL_W)left=right=0;
        s->label_opaque_span[row][0]=(uint8_t)left;s->label_opaque_span[row][1]=(uint8_t)right;
    }
    s->label_opaque_valid=true;
}
/* Only sparse normal-frame restoration uses this path. The label pass below
 * overwrites every skipped pixel before any frame is submitted. A glint drawn
 * first is pointwise and is overwritten there too; the clean background never
 * contains a label, actor or trail. Lazy tile/full restores remain complete. */
static void restore_row_span(adaptive_scene_t *s,unsigned x,unsigned y,unsigned width){
    unsigned end=x+width;
    if(s->label_opaque_valid&&y>=ADAPTIVE_LABEL_Y&&y-ADAPTIVE_LABEL_Y<ADAPTIVE_LABEL_H){
        const uint8_t *span=s->label_opaque_span[y-ADAPTIVE_LABEL_Y];
        unsigned left=ADAPTIVE_LABEL_X+span[0],right=ADAPTIVE_LABEL_X+span[1];
        if(left<right&&x<right&&end>left){
            if(x<left)memcpy(s->canvas+y*s->stride+x,s->background+y*480+x,(left-x)*2);
            if(end>right)memcpy(s->canvas+y*s->stride+right,s->background+y*480+right,(end-right)*2);
            return;
        }
    }
    memcpy(s->canvas+y*s->stride+x,s->background+y*480+x,width*2);
}
/* Adjacent dirty tiles share a scanline in both backing planes. Restore
 * those runs with one contiguous copy per scanline rather than 32 tiny copies
 * for every tile. The same tile flags and pixel addresses are restored. */
static bool restore_occupied(adaptive_scene_t *s){
    unsigned count=0;
    for(unsigned i=0;i<SCENE_TILES;i++)count+=s->occupied[i]!=0;
    s->profile.restore_tiles+=count;
    if(s->dense_copy_enabled&&count>=96&&s->environment_copy&&s->environment_copy(s->environment_copy_context,
            s->background,480,s->canvas,s->stride)){
        /* Only the old ink needs display invalidation. Pixels outside the old
         * occupied set already equal background; the full copy preserves them. */
        for(unsigned i=0;i<SCENE_TILES;i++)s->dirty[i]|=s->occupied[i];
        memset(s->prepared,1,sizeof(s->prepared));
        memset(s->occupied,0,sizeof(s->occupied));return true;
    }
    for(unsigned ty=0;ty<15;ty++){
        unsigned tx=0;
        while(tx<15){
            if(!s->occupied[ty*15+tx]){tx++;continue;}
            unsigned first=tx;
            while(tx<15&&s->occupied[ty*15+tx])tx++;
            unsigned x=first*32,width=(tx-first)*32,y=ty*32;
            for(unsigned row=0;row<32;row++)restore_row_span(s,x,y+row,width);
            memset(s->dirty+ty*15+first,1,tx-first);
            memset(s->prepared+ty*15+first,1,tx-first);
        }
    }
    return false;
}
static inline __attribute__((always_inline)) void prepare(adaptive_scene_t *s,unsigned x,unsigned y){
    unsigned tile=y/32*15+x/32;
    if(!s->prepared[tile])restore(s,tile);
    s->occupied[tile]=1;
}
static inline __attribute__((always_inline)) uint16_t blend(uint16_t old,uint16_t colour,unsigned alpha){
    if(alpha>=255)return colour;
    unsigned inv=255-alpha;
    unsigned r=(((colour>>11)&31)*alpha+((old>>11)&31)*inv+127)/255;
    unsigned g=(((colour>>5)&63)*alpha+((old>>5)&63)*inv+127)/255;
    unsigned b=((colour&31)*alpha+(old&31)*inv+127)/255;
    return (uint16_t)((r<<11)|(g<<5)|b);
}
static inline __attribute__((always_inline)) void pixel(adaptive_scene_t *s,int x,int y,uint16_t colour,unsigned alpha){
    if(x<0||y<0||x>=480||y>=480||!alpha)return;
    prepare(s,(unsigned)x,(unsigned)y);
    uint16_t *p=s->canvas+y*s->stride+x;*p=blend(*p,colour,alpha);
}
static inline __attribute__((always_inline)) void character_pixel(adaptive_scene_t *s,int x,int y,
        uint16_t colour,unsigned alpha,unsigned *last_tile){
    if(x<0||y<0||x>=480||y>=480||!alpha)return;
    unsigned tile=(unsigned)y/32*15+(unsigned)x/32;
    if(tile!=*last_tile){
        if(!s->prepared[tile])restore(s,tile);
        s->occupied[tile]=1;*last_tile=tile;
    }
    uint16_t *out=s->canvas+(unsigned)y*s->stride+(unsigned)x;
    if(alpha>=255)*out=colour;
    else *out=blend(*out,colour,alpha);
}

/* The procedural layers draw before trails and the character. The physical
 * record is fixed; transparent controls are composited afterwards. */
static void scenery_plot(void *context,int x,int y,uint16_t colour,unsigned alpha){
    adaptive_scene_t *s=(adaptive_scene_t*)context;
    if(x<0||y<0||x>=480||y>=480||!alpha)return;
    if((unsigned)x>=s->scenery_inside[y][0]&&(unsigned)x<s->scenery_inside[y][1])return;
    pixel(s,x,y,colour,alpha);s->ambience_pixels++;
}
static bool sprite(adaptive_scene_t *s,const adaptive_asset_t *a,int x,int y,unsigned skin){
    if(a->raw!=(size_t)a->w*a->h*2||skin>=ADAPTIVE_SKINS||!decode(s,a))return false;
    for(unsigned i=0;i<(unsigned)a->w*a->h;i++)if(s->scratch[i*2+1]&&s->scratch[i*2]>=ADAPTIVE_PALETTE_COLORS)return false;
    for(unsigned row=0;row<a->h;row++)for(unsigned col=0;col<a->w;col++){
        unsigned i=(row*a->w+col)*2,alpha=s->scratch[i+1];
        if(alpha)pixel(s,x+col,y+row,adaptive_palettes[skin][s->scratch[i]],alpha);
    }
    return true;
}
static unsigned sprite_frame(const motion_pose_t *q){
    const adaptive_clip_t *clip=&adaptive_clips[q->clip];
    return clip->first+(unsigned)lroundf(clampf(q->phase,0,1)*(clip->count-1));
}
#if SCENE_PC_SMOOTH_CHARACTERS
enum {CHARACTER_HISTORY_STEPS=60,CHARACTER_HISTORY_COUNT=61};
bool adaptive_scene_secondary_motion(const motion_timeline_t *timeline,uint64_t samples,
                                      skating_secondary_motion_t *secondary){
    if(!secondary)return false;
    skating_motion_sample_t history[CHARACTER_HISTORY_COUNT];
    /* Reconstruct one second at 60 Hz from the audible sample clock. Replaying
     * a bounded history makes seeking and paused previews independent of
     * previously rendered frames. Keep absolute samples across song loops. */
    for(unsigned i=0;i<CHARACTER_HISTORY_COUNT;i++){
        uint64_t offset=((uint64_t)(CHARACTER_HISTORY_STEPS-i)*SONG_SAMPLE_RATE
                         +CHARACTER_HISTORY_STEPS/2)/CHARACTER_HISTORY_STEPS;
        uint64_t sample=samples>=offset?samples-offset:0;
        motion_pose_t q;
        if(!motion_evaluate(timeline,sample,&q)||q.clip>=ADAPTIVE_CLIPS)return false;
        const adaptive_clip_t *clip=&adaptive_clips[q.clip];
        if(!clip->count)return false;
        float position=clampf(q.phase,0,1)*(clip->count-1);
        unsigned first=(unsigned)floorf(position),second=first+1<clip->count?first+1:first;
        history[i]=(skating_motion_sample_t){
            .frame0=clip->first+first,.frame1=clip->first+second,
            .blend=position-first,.u=q.u,.v=q.v};
    }
    return skating_character_secondary_motion(history,CHARACTER_HISTORY_COUNT,
                                               1.0f/CHARACTER_HISTORY_STEPS,secondary);
}
#endif
static adaptive_footprint_t visible_footprint(const motion_pose_t *q,unsigned skin){
    const adaptive_clip_t *clip=&adaptive_clips[q->clip];
#if SCENE_PC_SMOOTH_CHARACTERS
    (void)skin;
#else
    if(skin<ADAPTIVE_SKINS)return adaptive_footprints[sprite_frame(q)];
#endif
    float position=clampf(q->phase,0,1)*(clip->count-1);
    unsigned left=(unsigned)floorf(position),right=left+1<clip->count?left+1:left;
    const adaptive_footprint_t *a=&adaptive_footprints[clip->first+left],*b=&adaptive_footprints[clip->first+right];
    float t=position-left;
    adaptive_footprint_t f;
#define LERP_FOOT(field) f.field=a->field+(b->field-a->field)*t
    LERP_FOOT(u_l);LERP_FOOT(v_l);LERP_FOOT(z_l);LERP_FOOT(load_l);
    LERP_FOOT(u_r);LERP_FOOT(v_r);LERP_FOOT(z_r);LERP_FOOT(load_r);
#undef LERP_FOOT
    return f;
}
/* Resample the existing compact palette art in premultiplied colour. Enlarging
 * the actor around its wheel-space origin keeps all three palettes registered
 * with the same enlarged contacts, without duplicating the flash-heavy atlas. */
static bool character_sprite_fallback(adaptive_scene_t *s,const adaptive_asset_t *a,int x,int y,unsigned skin){
    if(a->raw!=(size_t)a->w*a->h*2||skin>=ADAPTIVE_SKINS||!decode(s,a))return false;
    for(unsigned i=0;i<(unsigned)a->w*a->h;i++)if(s->scratch[i*2+1]&&s->scratch[i*2]>=ADAPTIVE_PALETTE_COLORS)return false;
    int left=(int)floorf(a->x*SCENE_CHARACTER_SCALE),right=(int)ceilf((a->x+a->w)*SCENE_CHARACTER_SCALE);
    int top=(int)floorf(a->y*SCENE_CHARACTER_SCALE),bottom=(int)ceilf((a->y+a->h)*SCENE_CHARACTER_SCALE);
    /* Every current enlarged sprite has <=161 columns. Keep a bounded stack
     * table and the original path for any future wider asset. */
    struct {int16_t index;uint16_t fraction;} columns[192];
    bool table=right-left<=192;
    if(table)for(int col=left;col<right;col++){
        float u=(col+.5f)/SCENE_CHARACTER_SCALE-a->x-.5f;
        int ix=(int)floorf(u);
        columns[col-left].index=(int16_t)ix;
        columns[col-left].fraction=(uint16_t)lroundf((u-ix)*256);
    }
    unsigned last_tile=UINT_MAX;
    for(int row=top;row<bottom;row++){
        float v=(row+.5f)/SCENE_CHARACTER_SCALE-a->y-.5f;
        int iy=(int)floorf(v);
        unsigned fy=(unsigned)lroundf((v-iy)*256);
        for(int col=left;col<right;col++){
            int ix;unsigned fx;
            if(table){ix=columns[col-left].index;fx=columns[col-left].fraction;}
            else{
                float u=(col+.5f)/SCENE_CHARACTER_SCALE-a->x-.5f;
                ix=(int)floorf(u);fx=(unsigned)lroundf((u-ix)*256);
            }
            if(ix>=0&&iy>=0&&ix+1<a->w&&iy+1<a->h){
                const uint8_t *p=s->scratch+((unsigned)iy*a->w+(unsigned)ix)*2;
                const uint8_t *n=p+a->w*2;
                unsigned a0=p[1],a1=p[3],a2=n[1],a3=n[3];
                if(!(a0|a1|a2|a3))continue;
                if(a0==a1&&a0==a2&&a0==a3){
                    /* Equal sample alphas cancel exactly from the original
                     * premultiplied divisions. The four weights sum to65536. */
                    uint16_t colour;
                    if(p[0]==p[2]&&p[0]==n[0]&&p[0]==n[2])colour=adaptive_palettes[skin][p[0]];
                    else{
                        const uint16_t *palette=adaptive_palettes[skin];
                        uint16_t c0=palette[p[0]],c1=palette[p[2]],c2=palette[n[0]],c3=palette[n[2]];
                        unsigned w0=(256-fx)*(256-fy),w1=fx*(256-fy),w2=(256-fx)*fy,w3=fx*fy;
                        unsigned r=((c0>>11)&31)*w0+((c1>>11)&31)*w1+((c2>>11)&31)*w2+((c3>>11)&31)*w3;
                        unsigned g=((c0>>5)&63)*w0+((c1>>5)&63)*w1+((c2>>5)&63)*w2+((c3>>5)&63)*w3;
                        unsigned b=(c0&31)*w0+(c1&31)*w1+(c2&31)*w2+(c3&31)*w3;
                        colour=(uint16_t)(((r>>16)<<11)|((g>>16)<<5)|(b>>16));
                    }
                    character_pixel(s,x+col,y+row,colour,a0,&last_tile);continue;
                }
            }
            unsigned alpha=0,r=0,g=0,b=0;
            for(unsigned j=0;j<2;j++)for(unsigned i=0;i<2;i++){
                int sx=ix+(int)i,sy=iy+(int)j;
                if(sx<0||sy<0||sx>=a->w||sy>=a->h)continue;
                unsigned p=((unsigned)sy*a->w+(unsigned)sx)*2;
                unsigned w=(i?fx:256-fx)*(j?fy:256-fy)*s->scratch[p+1];
                if(!w)continue;
                uint16_t c=adaptive_palettes[skin][s->scratch[p]];
                alpha+=w;r+=((c>>11)&31)*w;g+=((c>>5)&63)*w;b+=(c&31)*w;
            }
            if(alpha){
                uint16_t c=(uint16_t)(((r/alpha)<<11)|((g/alpha)<<5)|(b/alpha));
                character_pixel(s,x+col,y+row,c,(alpha+32768)>>16,&last_tile);
            }
        }
    }
    return true;
}

typedef struct {int16_t index;uint16_t fraction;} character_axis_t;
static void character_horizontal(adaptive_scene_t *s,const adaptive_asset_t *a,int row,
        const character_axis_t *axis,unsigned count,uint32_t *out,unsigned skin){
    if(row<0||row>=a->h){memset(out,0,count*4*sizeof(uint32_t));return;}
    const uint16_t *palette=adaptive_palettes[skin];
    for(unsigned col=0;col<count;col++){
        int x=axis[col].index;unsigned fx=axis[col].fraction;
        unsigned alpha=0,r=0,g=0,b=0;
        for(unsigned side=0;side<2;side++){
            int sx=x+(int)side;if(sx<0||sx>=a->w)continue;
            unsigned index=((unsigned)row*a->w+(unsigned)sx)*2;
            unsigned w=(side?fx:256-fx)*s->scratch[index+1];if(!w)continue;
            uint16_t c=palette[s->scratch[index]];
            alpha+=w;r+=((c>>11)&31)*w;g+=((c>>5)&63)*w;b+=(c&31)*w;
        }
        out[col*4]=alpha;out[col*4+1]=r;out[col*4+2]=g;out[col*4+3]=b;
    }
}
static bool character_sprite(adaptive_scene_t *s,const adaptive_asset_t *a,int x,int y,unsigned skin){
    int left=(int)floorf(a->x*SCENE_CHARACTER_SCALE),right=(int)ceilf((a->x+a->w)*SCENE_CHARACTER_SCALE);
    int top=(int)floorf(a->y*SCENE_CHARACTER_SCALE),bottom=(int)ceilf((a->y+a->h)*SCENE_CHARACTER_SCALE);
    unsigned count=right>left?(unsigned)(right-left):0;
    size_t offset=(a->raw+3u)&~(size_t)3u,row_bytes=(size_t)count*4*sizeof(uint32_t);
    if(!count||count>256||offset>s->main_capacity||row_bytes*2>s->main_capacity-offset)
        return character_sprite_fallback(s,a,x,y,skin);
    if(a->raw!=(size_t)a->w*a->h*2||skin>=ADAPTIVE_SKINS||!decode(s,a))return false;
    for(unsigned i=0;i<(unsigned)a->w*a->h;i++)if(s->scratch[i*2+1]&&s->scratch[i*2]>=ADAPTIVE_PALETTE_COLORS)return false;
    character_axis_t axis[256];
    for(int col=left;col<right;col++){
        float u=(col+.5f)/SCENE_CHARACTER_SCALE-a->x-.5f;int ix=(int)floorf(u);
        axis[col-left]=(character_axis_t){(int16_t)ix,(uint16_t)lroundf((u-ix)*256)};
    }
    uint32_t *rows[2]={(uint32_t*)(void*)(s->scratch+offset),(uint32_t*)(void*)(s->scratch+offset+row_bytes)};
    int cached[2]={INT_MIN,INT_MIN};unsigned last_tile=UINT_MAX;
    for(int row=top;row<bottom;row++){
        if(y+row<0||y+row>=480)continue;
        float v=(row+.5f)/SCENE_CHARACTER_SCALE-a->y-.5f;int iy=(int)floorf(v);
        unsigned fy=(unsigned)lroundf((v-iy)*256),wy0=256-fy;
        for(unsigned side=0;side<2;side++){
            int sy=iy+(int)side;unsigned slot=(unsigned)sy&1u;
            if(cached[slot]!=sy){character_horizontal(s,a,sy,axis,count,rows[slot],skin);cached[slot]=sy;}
        }
        const uint32_t *a0=rows[(unsigned)iy&1u],*a1=rows[(unsigned)(iy+1)&1u];
        for(int col=left;col<right;col++){
            if(x+col<0||x+col>=480)continue;
            unsigned i=(unsigned)(col-left)*4;
            unsigned alpha=a0[i]*wy0+a1[i]*fy;
            if(alpha<32768)continue;
            unsigned r=a0[i+1]*wy0+a1[i+1]*fy,g=a0[i+2]*wy0+a1[i+2]*fy,b=a0[i+3]*wy0+a1[i+3]*fy;
            uint16_t colour;
            if(alpha==255u*65536u)colour=(uint16_t)((((r>>16)/255)<<11)|(((g>>16)/255)<<5)|((b>>16)/255));
            else colour=(uint16_t)(((r/alpha)<<11)|((g/alpha)<<5)|(b/alpha));
            character_pixel(s,x+col,y+row,colour,(alpha+32768)>>16,&last_tile);
        }
    }
    return true;
}

typedef struct {float x,y,width,alpha;bool contact;unsigned frame,plan;} mark_t;
typedef struct {int left,right,top,bottom;} trail_bounds_t;
static bool wheel_contact(const adaptive_footprint_t *f,unsigned foot){
    return (foot?f->z_r:f->z_l)<SCENE_CONTACT_HEIGHT&&(f->load_l+f->load_r)>.001f;
}
static float foot_channel(const adaptive_footprint_t *f,unsigned foot,unsigned channel){
    if(channel==0)return foot?f->u_r:f->u_l;
    if(channel==1)return foot?f->v_r:f->v_l;
    if(channel==2)return foot?f->load_r:f->load_l;
    return foot?f->z_l:f->z_r; /* height of the other skate: width only */
}
static float monotone_slope(float a,float b){
    return a*b>0?2*a*b/(a+b):0;
}
static void continuous_wheel(const motion_pose_t *q,unsigned index,unsigned foot,float out[4]){
    const adaptive_clip_t *clip=&adaptive_clips[q->clip];
    const adaptive_footprint_t *visible=&adaptive_footprints[index];
    for(unsigned c=0;c<4;c++)out[c]=foot_channel(visible,foot,c);
    float position=clampf(q->phase,0,1)*(clip->count-1);
    unsigned left=(unsigned)floorf(position),right=left+1;
    if(right>=clip->count)return;
    const adaptive_footprint_t *a=&adaptive_footprints[clip->first+left],*b=a+1;
    /* The contact mask and landing clock still belong to the visible sprite.
     * Never fit through a lifted skate, across a jump or into another clip. */
    if(!wheel_contact(a,foot)||!wheel_contact(b,foot))return;
    const adaptive_footprint_t *before=left&&wheel_contact(a-1,foot)?a-1:a;
    const adaptive_footprint_t *after=right+1<clip->count&&wheel_contact(b+1,foot)?b+1:b;
    float t=position-left,t2=t*t,t3=t2*t;
    for(unsigned c=0;c<4;c++){
        float av=foot_channel(a,foot,c),bv=foot_channel(b,foot,c),delta=bv-av;
        if(c<2){
            float m0=before==a?delta:monotone_slope(av-foot_channel(before,foot,c),delta);
            float m1=after==b?delta:monotone_slope(delta,foot_channel(after,foot,c)-bv);
            out[c]=(2*t3-3*t2+1)*av+(t3-2*t2+t)*m0+(-2*t3+3*t2)*bv+(t3-t2)*m1;
        }else out[c]=av+delta*t;
    }
}
static float loudness_at(const motion_timeline_t *t,uint64_t samples){
    const song_audio_features_t *a=&t->audio;
    if(!a->envelope||!a->envelope_count||!a->envelope_hop)return -1;
    uint32_t sample=(uint32_t)(samples%t->sample_count);
    unsigned index=sample/a->envelope_hop;
    if(index>=a->envelope_count-1)return a->envelope[a->envelope_count-1]/255.0f;
    float fraction=(float)(sample%a->envelope_hop)/a->envelope_hop;
    return (a->envelope[index]+(a->envelope[index+1]-a->envelope[index])*fraction)/255.0f;
}
static bool continuous_contact(const motion_timeline_t *t,const mark_t *a,const mark_t *b,unsigned foot){
    if(a->plan==b->plan&&b->frame>=a->frame){
        for(unsigned i=a->frame+1;i<b->frame;i++)
            if(!wheel_contact(&adaptive_footprints[i],foot))return false;
        return true;
    }
    bool adjacent=a->plan+1==b->plan||(a->plan+1==t->plan_count&&b->plan==0);
    if(!adjacent)return false;
    const adaptive_clip_t *old=&adaptive_clips[t->plans[a->plan].clip];
    const adaptive_clip_t *next=&adaptive_clips[t->plans[b->plan].clip];
    for(unsigned i=a->frame+1;i<old->first+old->count;i++)
        if(!wheel_contact(&adaptive_footprints[i],foot))return false;
    for(unsigned i=next->first;i<b->frame;i++)
        if(!wheel_contact(&adaptive_footprints[i],foot))return false;
    return true;
}
static inline float finite_min(float a,float b){return a<b?a:b;}
static inline float finite_max(float a,float b){return a>b?a:b;}
static inline int finite_floor(float x){int i=(int)x;return i-(i>x);}
static inline int finite_ceil(float x){int i=(int)x;return i+(i<x);}
/* The low15 bits cover the480-pixel row. Return consecutive set blocks in
 * ascending x order; neither ctz input can be zero while a mask is nonzero. */
static inline void trail_block_run(unsigned *mask,unsigned *left,unsigned *right){
    unsigned first=(unsigned)__builtin_ctz(*mask);
    unsigned end=first+(unsigned)__builtin_ctz(~(*mask>>first));
    *mask&=~((1u<<end)-1u);*left=first*32u;*right=end*32u;
}
static void capsule(adaptive_scene_t *s,const mark_t *a,const mark_t *b,trail_bounds_t *bounds){
    float dx=b->x-a->x,dy=b->y-a->y,length=dx*dx+dy*dy;
    float inv_length=length>.0001f?1/length:0;
    float limit=finite_max(a->width,b->width)*.5f+.7f;
    int left=finite_floor(finite_min(a->x,b->x)-limit),right=finite_ceil(finite_max(a->x,b->x)+limit);
    int top=finite_floor(finite_min(a->y,b->y)-limit),bottom=finite_ceil(finite_max(a->y,b->y)+limit);
    left=left<0?0:left;right=right>479?479:right;top=top<0?0:top;bottom=bottom>479?479:bottom;
    if(left>right||top>bottom)return;
    if(left<bounds->left)bounds->left=left;
    if(right>bounds->right)bounds->right=right;
    if(top<bounds->top)bounds->top=top;
    if(bottom>bounds->bottom)bounds->bottom=bottom;
    bool cull_rows=fabsf(dy)>.001f&&fabsf(dx)>limit*2;
    float slope=0,band=0;
    if(cull_rows){slope=dx/dy;band=(limit+1)*sqrtf(length)/fabsf(dy);}
    for(int y=top;y<=bottom;y++){
        int row_left=left,row_right=right;
        /* An infinite strip around the segment is a conservative row bound.
         * Its extra full pixel encloses every original float32 edge test;
         * endpoint clipping and the coverage calculation stay unchanged. */
        if(cull_rows){
            float mid=a->x+slope*(y+.5f-a->y);
            int aleft=finite_floor(mid-band),aright=finite_ceil(mid+band);
            if(aleft>row_left)row_left=aleft;
            if(aright<row_right)row_right=aright;
        }
        if(row_left>row_right)continue;
        unsigned first_block=(unsigned)row_left/32u,last_block=(unsigned)row_right/32u;
        s->trail_row_mask[y]|=(uint16_t)(((1u<<(last_block+1u))-1u)&~((1u<<first_block)-1u));
        if((unsigned)row_left<s->trail_row_left[y])s->trail_row_left[y]=(uint16_t)row_left;
        if((unsigned)row_right+1>s->trail_row_right[y])s->trail_row_right[y]=(uint16_t)(row_right+1);
        s->profile.trail_raster_tests+=(unsigned)(row_right-row_left+1);
        for(int x=row_left;x<=row_right;x++){
        float px=x+.5f-a->x,py=y+.5f-a->y;
        float t=length>.0001f?clampf((px*dx+py*dy)*inv_length,0,1):0;
        float ex=px-t*dx,ey=py-t*dy,d2=ex*ex+ey*ey;
        float r=(a->width+(b->width-a->width)*t)*.5f;
        if(d2>(r+.7f)*(r+.7f))continue;
        float core=r-.301f;
        float coverage=core>0&&d2<core*core?1:clampf(r+.7f-sqrtf(d2),0,1);
        unsigned alpha=(unsigned)lroundf((a->alpha+(b->alpha-a->alpha)*t)*coverage);
        unsigned index=(unsigned)y*480+(unsigned)x;
        if(alpha>s->trail_alpha[index])s->trail_alpha[index]=(uint8_t)alpha;
        }
    }
}
void adaptive_scene_motion_cache_reset(adaptive_scene_t *s){
    if(!s)return;
    s->motion_cache_valid=false;
    for(unsigned i=0;i<16;i++)s->landing_sample_cache[i].frame=UINT_MAX;
    for(unsigned i=0;i<SCENE_TRAIL_POINTS-1;i++)s->motion_cache[i].sample=UINT64_MAX;
}
static void motion_cache_bind(adaptive_scene_t *s,const motion_timeline_t *t,uint64_t samples){
    const motion_timeline_t *old=&s->motion_cache_timeline;
    if(!s->motion_cache_valid||s->last_samples==UINT64_MAX||samples<s->last_samples
       ||t->sample_count!=old->sample_count||t->beat_count!=old->beat_count
       ||t->plan_count!=old->plan_count||t->beats!=old->beats||t->plans!=old->plans
       ||t->audio.envelope_hop!=old->audio.envelope_hop
       ||t->audio.envelope_count!=old->audio.envelope_count
       ||t->audio.drum_count!=old->audio.drum_count
       ||t->audio.envelope!=old->audio.envelope||t->audio.drums!=old->audio.drums){
        adaptive_scene_motion_cache_reset(s);
        s->motion_cache_timeline=*t;s->motion_cache_valid=true;
    }
}
static bool cache_wheels(adaptive_scene_motion_cache_t *entry,const motion_timeline_t *t,uint64_t sample){
    const motion_pose_t *q=&entry->pose;
    entry->frame=sprite_frame(q);if(entry->frame>=ADAPTIVE_SPRITES)return false;
    const adaptive_footprint_t *f=&adaptive_footprints[entry->frame];entry->contact_mask=0;
    float level=loudness_at(t,sample);entry->base=level<0?2.55f:.85f+4.55f*level;
    for(unsigned foot=0;foot<2;foot++){
        if(!wheel_contact(f,foot))continue;
        entry->contact_mask|=1u<<foot;
        float wheel[4];continuous_wheel(q,entry->frame,foot,wheel);
        entry->u[foot]=q->u+wheel[0]*SCENE_CHARACTER_SCALE;
        entry->v[foot]=q->v+wheel[1]*SCENE_CHARACTER_SCALE;
        entry->contact[foot]=1-smoothf((foot?f->z_r:f->z_l)/SCENE_CONTACT_HEIGHT);
        entry->pressure[foot]=.7f*wheel[2]+.2f*smoothf(wheel[3]/1.3f)+.1f;
    }
    entry->sample=sample;return true;
}
static adaptive_scene_motion_cache_t *cached_motion(adaptive_scene_t *s,const motion_timeline_t *t,
                                                    uint64_t sample,bool lattice,adaptive_scene_motion_cache_t *live){
    adaptive_scene_motion_cache_t *entry=live;
    if(lattice){
        entry=&s->motion_cache[(sample/224)%(SCENE_TRAIL_POINTS-1)];
        if(entry->sample==sample){s->profile.trail_cache_hits++;return entry;}
    }
    s->profile.motion_evaluations++;
    if(!motion_evaluate(t,sample,&entry->pose)||!cache_wheels(entry,t,sample))return NULL;
    return entry;
}
typedef struct {mark_t a,b;} trail_segment_t;
_Static_assert(sizeof(trail_segment_t)*SCENE_TRAIL_POINTS*2<=SCENE_MAIN_SCRATCH_BYTES,"trail segment workspace");
static bool trails(adaptive_scene_t *s,const motion_timeline_t *timeline,uint64_t samples,unsigned skin){
    uint32_t stage_start=scene_now_us();
    if(s->main_capacity<sizeof(trail_segment_t)*SCENE_TRAIL_POINTS*2)return false;
    trail_segment_t *segments=(trail_segment_t*)s->scratch;unsigned segment_count=0;
    s->profile.trail_clear_bytes=s->profile.trail_raster_tests=0;
    if(!s->trail_rows_valid){
        memset(s->trail_alpha,0,480*480);s->trail_rows_valid=true;s->profile.trail_clear_bytes=480*480;
    }else for(unsigned y=0;y<SCENE_HEIGHT;y++){
        unsigned mask=s->trail_row_mask[y];
        while(mask){
            unsigned left,right;trail_block_run(&mask,&left,&right);
            if(left<s->trail_row_left[y])left=s->trail_row_left[y];
            if(right>s->trail_row_right[y])right=s->trail_row_right[y];
            if(left<right){memset(s->trail_alpha+y*480+left,0,right-left);s->profile.trail_clear_bytes+=right-left;}
        }
    }
    for(unsigned y=0;y<SCENE_HEIGHT;y++){
        s->trail_row_left[y]=480;s->trail_row_right[y]=0;s->trail_row_mask[y]=0;
    }
    const unsigned count=SCENE_TRAIL_POINTS,step=224;
    const uint64_t grid=samples-samples%step;
    const float lifetime=(SCENE_TRAIL_POINTS-1)*224.0f/16000;
    mark_t previous[2]={{0}},history[2][5]={{{0}}};unsigned used[2]={0};
    uint64_t previous_sample[2]={0};
    trail_bounds_t bounds={480,-1,480,-1};
    s->trail_min_width=100;s->trail_max_width=0;s->trail_air_samples=0;s->trail_current_mask=0;
    s->audio_level=loudness_at(timeline,samples);
    for(unsigned j=0;j<count;j++){
        if(j+1==count&&samples==grid)break; /* grid point already is the head */
        /* Historical deposits stay on an absolute audio-sample lattice.
         * Rephasing every old point each display frame makes the whole ink
         * line shimmer, even though it was already left on the vinyl. */
        uint64_t at=samples;
        if(j+1<count){
            uint64_t back=(uint64_t)(count-2-j)*step;
            if(back>grid)continue;
            at=grid-back;
        }
        float age=(float)(samples-at)/16000;
        float fresh=clampf(1-age/lifetime,0,1),fade_position=fresh*(count-1);
        unsigned fi=(unsigned)floorf(fade_position),fj=fi+1<count?fi+1:fi;
        float fraction=fade_position-fi;
        float taper=s->trail_taper[fi]+fraction*(s->trail_taper[fj]-s->trail_taper[fi]);
        float fade=s->trail_fade[fi]+fraction*(s->trail_fade[fj]-s->trail_fade[fi]);
        adaptive_scene_motion_cache_t live;
        const adaptive_scene_motion_cache_t *entry=cached_motion(s,timeline,at,at%step==0,&live);
        if(!entry)return false;
        const motion_pose_t *q=&entry->pose;unsigned index=entry->frame;
        float angle=-2*PI*age/1.6f,ca=cosf(angle),sa=sinf(angle);
        for(unsigned foot=0;foot<2;foot++){
            /* Both loads are zero while the whole body is airborne. A light
             * toe tap still has wheel contact even with negligible load. */
            mark_t p={.contact=(entry->contact_mask&(1u<<foot))!=0,.frame=index,.plan=q->plan};
            if(!p.contact){previous[foot].contact=false;used[foot]=0;s->trail_air_samples++;continue;}
            float uu=entry->u[foot],vv=entry->v[foot];
            /* Keep the newest ink attached to the actually drawn skate.
             * Only this final <=14ms tip follows the displayed pose; older
             * deposits retain their smooth, fixed historical coordinates. */
            if(at==samples){
                adaptive_footprint_t visible=visible_footprint(q,skin);
                uu=q->u+foot_channel(&visible,foot,0)*SCENE_CHARACTER_SCALE;
                vv=q->v+foot_channel(&visible,foot,1)*SCENE_CHARACTER_SCALE;
            }
            project(uu*ca-vv*sa,uu*sa+vv*ca,0,&p.x,&p.y);
            float contact=entry->contact[foot],pressure=entry->pressure[foot];
            bool connected=previous[foot].contact&&continuous_contact(timeline,&previous[foot],&p,foot);
            float bend=0,speed=0;
            if(connected){
                float dx=p.x-previous[foot].x,dy=p.y-previous[foot].y;
                float distance=sqrtf(dx*dx+dy*dy);
                speed=at>previous_sample[foot]?distance*16000/(float)(at-previous_sample[foot]):0;
                if(used[foot]>=5){
                    /* Wider tangent windows reject frame-quantization noise
                     * without flattening the intentional S and turn routes. */
                    float ax=history[foot][2].x-history[foot][4].x;
                    float ay=history[foot][2].y-history[foot][4].y;
                    float bx=p.x-history[foot][1].x,by=p.y-history[foot][1].y;
                    float aa=ax*ax+ay*ay,bb=bx*bx+by*by;
                    if(aa>.01f&&bb>.01f) bend=clampf(fabsf(ax*by-ay*bx)/sqrtf(aa*bb)*1.5f,0,1);
                }
            }else used[foot]=0;
            /* The wheels, load, and local turning rate control the mark.
             * A loaded sharp edge leaves a broad cut; a light fast wheel
             * leaves a finer line. This is evaluated from the visible feet. */
            float scrape=clampf((speed-250)/450,0,1);
            float base=entry->base;
            p.width=clampf(base*(.78f+.22f*pressure)*(1+.28f*bend)*(1-.08f*scrape)*taper*(.88f+.12f*contact),.6f,6.4f);
            if(p.width<s->trail_min_width)s->trail_min_width=p.width;
            if(p.width>s->trail_max_width)s->trail_max_width=p.width;
            p.alpha=fade*(.58f+.42f*contact);
            if(segment_count>=SCENE_TRAIL_POINTS*2)return false;
            segments[segment_count++]=(trail_segment_t){.a=connected?previous[foot]:p,.b=p};
            if(at==samples)s->trail_current_mask|=1u<<foot;
            for(unsigned k=4;k>0;k--)history[foot][k]=history[foot][k-1];
            history[foot][0]=p;if(used[foot]<5)used[foot]++;previous[foot]=p;previous_sample[foot]=at;
        }
    }
    s->profile.trail_segment_count=segment_count;
    s->profile.trail_setup_us=scene_now_us()-stage_start;stage_start=scene_now_us();
    for(unsigned i=0;i<segment_count;i++)capsule(s,&segments[i].a,&segments[i].b,&bounds);
    s->profile.trail_raster_us=scene_now_us()-stage_start;stage_start=scene_now_us();
    const uint16_t colour=trail_colour(s->last_theme);
    /* Every nonzero mark lies inside the capsule union. Avoid reading the
     * untouched part of the PSRAM alpha plane once per display frame. */
    for(int y=bounds.top;y<=bounds.bottom;y++){
        unsigned mask=s->trail_row_mask[y],prepared_tile=UINT_MAX;
        while(mask){
            unsigned left,right;trail_block_run(&mask,&left,&right);
            if(left<s->trail_row_left[y])left=s->trail_row_left[y];
            if(right>s->trail_row_right[y])right=s->trail_row_right[y];
            for(unsigned x=left;x<right;x++){
                unsigned alpha=s->trail_alpha[y*480+x];
                if(!alpha)continue;
                unsigned tile=(unsigned)y/32*15+x/32;
                if(tile!=prepared_tile){prepare(s,x,(unsigned)y);prepared_tile=tile;}
                uint16_t *out=s->canvas+(unsigned)y*s->stride+x;
                *out=blend(*out,colour,alpha);
            }
        }
    }
    s->profile.trail_composite_us=scene_now_us()-stage_start;
    return true;
}
static uint32_t noise(uint32_t x){x^=x>>16;x*=0x7feb352du;x^=x>>15;x*=0x846ca68bu;return x^(x>>16);}
static float random_unit(uint32_t x){return (noise(x)&65535)/65535.0f;}
/* First audible sample that selects this actual sprite frame. Searching the
 * observed-beat evaluator keeps landings exact through variable tempo, seeks,
 * pauses, and dropped display frames. There is no simulation-time RNG/state. */
static uint32_t frame_sample(adaptive_scene_t *s,const motion_timeline_t *t,unsigned p,unsigned frame){
    unsigned slot=(frame+p*31u)%16;
    if(s->landing_sample_cache[slot].plan==p&&s->landing_sample_cache[slot].frame==frame){
        s->profile.landing_cache_hits++;return s->landing_sample_cache[slot].sample;
    }
    uint32_t lo=t->plans[p].start_sample,hi=t->plans[p].end_sample-1;
    while(lo<hi){
        uint32_t mid=lo+(hi-lo)/2;motion_pose_t q;
        s->profile.motion_evaluations++;
        if(!motion_evaluate(t,mid,&q))return hi;
        if(sprite_frame(&q)<frame)lo=mid+1;else hi=mid;
    }
    s->landing_sample_cache[slot].plan=p;s->landing_sample_cache[slot].frame=frame;
    s->landing_sample_cache[slot].sample=lo;
    return lo;
}
typedef struct {adaptive_scene_t *scene;unsigned layer;} effect_plot_context_t;
static void effect_pixel(void *user,int x,int y,uint16_t colour,unsigned alpha){
    effect_plot_context_t *context=user;
    if(x<0||y<0||x>=480||y>=480||!alpha)return;
    adaptive_scene_t *s=context->scene;
    pixel(s,x,y,colour,alpha);s->effect_pixels++;
    if(context->layer)s->effect_front_pixels++;else s->effect_back_pixels++;
}
static float effect_drum_at(const motion_timeline_t *t,uint64_t samples){
    uint32_t position=(uint32_t)(samples%t->sample_count);
    uint64_t base=samples-position,absolute=0;unsigned strength=0;
    unsigned count=t->audio.drum_count?t->audio.drum_count:t->beat_count,lo=0,hi=count;
    if(!count)return 0;
    while(lo<hi){
        unsigned mid=lo+(hi-lo)/2;
        uint32_t at=t->audio.drum_count?t->audio.drums[mid].sample:t->beats[mid].sample;
        if(at<=position)lo=mid+1;else hi=mid;
    }
    unsigned index;
    if(lo)index=lo-1;
    else if(base>=t->sample_count){index=count-1;base-=t->sample_count;}
    else return 0;
    if(t->audio.drum_count){
        absolute=base+t->audio.drums[index].sample;strength=t->audio.drums[index].strength;
    }else{
        if(t->beats[index].flags&SONG_BEAT_SENTINEL)return 0;
        absolute=base+t->beats[index].sample;strength=t->beats[index].strength;
    }
    if(absolute>samples||samples-absolute>=3840)return 0;
    float fade=1-(float)(samples-absolute)/3840;
    return (float)strength/65535*fade*fade;
}
static void effect_foot_position(const motion_pose_t *q,const adaptive_footprint_t *f,unsigned foot,
                                 float z,float *x,float *y){
    float ax,ay;project(q->u,q->v,0,&ax,&ay);
    project((foot?f->u_r:f->u_l)*SCENE_CHARACTER_SCALE,(foot?f->v_r:f->v_l)*SCENE_CHARACTER_SCALE,z*SCENE_CHARACTER_SCALE,x,y);
    /* Desktop meshes retain the fractional root position before rasterizing;
     * the native sprite/worker path keeps its existing integer anchor. */
#if SCENE_PC_SMOOTH_CHARACTERS
    *x+=ax-195.56f;*y+=ay-303.36f;
#else
    *x+=(float)lroundf(ax)-195.56f;*y+=(float)lroundf(ay)-303.36f;
#endif
}
static bool effect_context(adaptive_scene_t *s,const motion_timeline_t *t,uint64_t samples,
                           const motion_pose_t *q,unsigned skin,foot_effect_context_t *fx){
    unsigned frame=sprite_frame(q);if(frame>=ADAPTIVE_SPRITES)return false;
    adaptive_footprint_t visible=visible_footprint(q,skin);
    const adaptive_footprint_t *f=&visible;
    *fx=(foot_effect_context_t){.samples=samples,.rms=s->audio_level<0?.35f:s->audio_level,
                               .drum_pulse=effect_drum_at(t,samples),.beat_phase=q->beat_phase};
    uint64_t before=samples>800?samples-800:0;motion_pose_t previous;
    if(samples>before)s->profile.motion_evaluations++;
    bool have_previous=samples>before&&motion_evaluate(t,before,&previous)&&previous.plan==q->plan;
    unsigned old_frame=have_previous?sprite_frame(&previous):0;
    have_previous=have_previous&&old_frame<ADAPTIVE_SPRITES;
    s->effect_contact_mask=0;s->effect_drum_pulse=fx->drum_pulse;
    for(unsigned foot=0;foot<2;foot++){
        foot_effect_foot_t *out=&fx->feet[foot];
        out->z_world=foot?f->z_r:f->z_l;out->load=foot?f->load_r:f->load_l;
        out->contact=wheel_contact(&adaptive_footprints[frame],foot);
        unsigned offset=foot*4;
        s->feet[offset]=q->u+(foot?f->u_r:f->u_l)*SCENE_CHARACTER_SCALE;
        s->feet[offset+1]=q->v+(foot?f->v_r:f->v_l)*SCENE_CHARACTER_SCALE;
        s->feet[offset+2]=out->z_world*SCENE_CHARACTER_SCALE;s->feet[offset+3]=out->load;
        if(out->contact)s->effect_contact_mask|=1u<<foot;
        effect_foot_position(q,f,foot,out->z_world,&out->x,&out->y);
        effect_foot_position(q,f,foot,0,&out->ground_x,&out->ground_y);
        if(have_previous){
            adaptive_footprint_t old=visible_footprint(&previous,skin);float x,y;
            effect_foot_position(&previous,&old,foot,foot?old.z_r:old.z_l,&x,&y);
            out->velocity_x=clampf((out->x-x)*16000/(float)(samples-before),-350,350);
            out->velocity_y=clampf((out->y-y)*16000/(float)(samples-before),-350,350);
        }
    }
    return true;
}
static void splash_stroke(adaptive_scene_t *s,float ax,float ay,float bx,float by,float width,unsigned alpha,uint16_t colour){
    float dx=bx-ax,dy=by-ay,length=dx*dx+dy*dy,r=width*.5f;
    int left=(int)floorf(fminf(ax,bx)-r-1),right=(int)ceilf(fmaxf(ax,bx)+r+1);
    int top=(int)floorf(fminf(ay,by)-r-1),bottom=(int)ceilf(fmaxf(ay,by)+r+1);
    left=left<0?0:left;right=right>479?479:right;top=top<0?0:top;bottom=bottom>479?479:bottom;
    for(int y=top;y<=bottom;y++)for(int x=left;x<=right;x++){
        float px=x+.5f-ax,py=y+.5f-ay,t=length>.0001f?clampf((px*dx+py*dy)/length,0,1):0;
        float ex=px-t*dx,ey=py-t*dy,d2=ex*ex+ey*ey;
        if(d2>(r+.65f)*(r+.65f))continue;
        unsigned coverage=(unsigned)lroundf(alpha*clampf(r+.65f-sqrtf(d2),0,1));
        if(coverage){
            pixel(s,x,y,colour,coverage);s->splash_pixels++;
            s->splash_sum_x+=(unsigned)x;s->splash_sum_y+=(unsigned)y;s->splash_alpha_sum+=coverage;
        }
    }
}
static void landing_spray(adaptive_scene_t *s,float u,float v,float age,float strength,uint32_t seed){
    const float life=.42f;
    static const uint16_t spray[SONG_THEME_COUNT]={
        RGB565(255,190,204),RGB565(255,223,157),
        RGB565(176,149,255),RGB565(246,179,116),
        RGB565(230,246,255),RGB565(226,255,178),RGB565(223,214,255)};
    static const uint16_t accents[SONG_THEME_COUNT]={
        RGB565(255,237,188),RGB565(255,148,118),
        RGB565(166,255,240),RGB565(255,239,190),
        RGB565(193,220,255),RGB565(255,232,158),RGB565(131,225,255)};
    const uint16_t rose=spray[s->last_theme];
    const uint16_t cream=accents[s->last_theme];
    float spin=-2*PI*age/1.6f,ca=cosf(spin),sa=sinf(spin);
    float fade=powf(clampf(1-age/life,0,1),1.4f);
    unsigned n=8+(unsigned)lroundf(6*strength);
    for(unsigned j=0;j<n;j++){
        float angle=2*PI*(j+(.35f+random_unit(seed+j*37)*.3f))/n;
        float velocity=(24+35*random_unit(seed+j*83+9))*strength;
        float travel=1.6f+velocity*age,tail=fmaxf(0,travel-1.0f-velocity*.018f);
        float pu=u+cosf(angle)*travel,pv=v+sinf(angle)*travel;
        float tu=u+cosf(angle)*tail,tv=v+sinf(angle)*tail;
        float x,y,tx,ty;project(pu*ca-pv*sa,pu*sa+pv*ca,0,&x,&y);
        project(tu*ca-tv*sa,tu*sa+tv*ca,0,&tx,&ty);
        unsigned alpha=(unsigned)lroundf((165+80*strength)*fade);
        splash_stroke(s,tx,ty,x,y,.95f+.6f*strength,alpha,j%3?rose:cream);
        s->splash_particles++;
    }
}
static bool landings(adaptive_scene_t *s,const motion_timeline_t *t,uint64_t samples,
                      const foot_effect_context_t *fx,effect_plot_context_t *plot){
    s->landing_events=s->splash_particles=s->splash_pixels=0;
    s->splash_sum_x=s->splash_sum_y=s->splash_alpha_sum=0;
    s->landing_age=-1;s->landing_u=s->landing_v=0;
    const uint64_t window=6720;
    uint64_t earliest=samples>window?samples-window:0;
    /* Normal songs are at least four seconds. Bound malformed / tiny native
     * test timelines to one cycle of effects instead of unbounded repeats. */
    if(samples-earliest>t->sample_count)earliest=samples-t->sample_count;
    uint64_t base=(earliest/t->sample_count)*t->sample_count;
    for(;;){
        uint32_t first=(uint32_t)(earliest>base?earliest-base:0);
        uint32_t last=(uint32_t)(samples-base<t->sample_count?samples-base:t->sample_count-1);
        unsigned begin=song_plan_find(t->plans,t->plan_count,first);
        for(unsigned p=begin;p<t->plan_count&&t->plans[p].start_sample<=last;p++){
            const adaptive_clip_t *clip=&adaptive_clips[t->plans[p].clip];
            for(unsigned k=1;k<clip->count;k++){
                unsigned index=clip->first+k;
                const adaptive_footprint_t *f=&adaptive_footprints[index];
                for(unsigned foot=0;foot<2;foot++){
                    if(!wheel_contact(f,foot)||wheel_contact(f-1,foot))continue;
                    float peak=0;
                    for(unsigned before=k;before>0;before--){
                        const adaptive_footprint_t *air=&adaptive_footprints[clip->first+before-1];
                        if(wheel_contact(air,foot))break;
                        peak=fmaxf(peak,foot?air->z_r:air->z_l);
                    }
                    if(peak<.75f)continue;
                    uint32_t sample=frame_sample(s,t,p,index);
                    uint64_t absolute=base+sample;
                    if(absolute<earliest||absolute>samples)continue;
                    s->profile.motion_evaluations++;
                    motion_pose_t q;if(!motion_evaluate(t,sample,&q))return false;
                    if(q.plan!=p||sprite_frame(&q)!=index)continue;
                    float age=(samples-absolute)/16000.0f;
                    if(age>=.42f)continue;
                    float u=q.u+(foot?f->u_r:f->u_l)*SCENE_CHARACTER_SCALE,v=q.v+(foot?f->v_r:f->v_l)*SCENE_CHARACTER_SCALE;
                    float load=foot?f->load_r:f->load_l;
                    float strength=clampf(.20f+.06f*peak+.24f*load,.25f,1);
                    if(q.clip==8)strength*=.65f;
                    s->landing_events++;s->landing_u=u;s->landing_v=v;s->landing_age=age;
                    landing_spray(s,u,v,age,strength,noise(sample^(p*101u+foot*1699u+index*71u)));
                    if(s->effect_mode!=FOOT_EFFECT_OFF){
                        float angle=-2*PI*age/1.6f,ca=cosf(angle),sa=sinf(angle),x,y;
                        project(u*ca-v*sa,u*sa+v*ca,0,&x,&y);
                        foot_effect_draw_landing(fx,s->effect_mode,x,y,age,strength,
                            noise(sample^(p*101u+foot*1699u+index*71u)),effect_pixel,plot);
                    }
                }
            }
        }
        if(base+t->sample_count>samples)break;
        base+=t->sample_count;
    }
    return true;
}
static bool draw_control(adaptive_scene_t *s,unsigned theme,bool playing){
    const adaptive_asset_t *a=&scene_controls[(theme-1)*SCENE_CONTROLS+(playing?1:0)];
    if(a->raw!=(unsigned)a->w*a->h*3
       ||!decode_nature(s,a->offset,a->packed,a->raw))return false;
    for(unsigned row=0;row<a->h;row++)for(unsigned col=0;col<a->w;col++){
        const uint8_t *p=s->scratch+(row*a->w+col)*3;
        pixel(s,a->x+col,a->y+row,(uint16_t)(p[0]|((unsigned)p[1]<<8)),p[2]);
    }
    return true;
}
static bool draw_record_details(adaptive_scene_t *s,unsigned theme,uint64_t samples,unsigned skin){
    unsigned glint=(unsigned)((samples%25600)*48/25600);
    const adaptive_asset_t *g=&adaptive_glints[glint];
    if(!sprite(s,g,g->x,g->y,skin<ADAPTIVE_SKINS?skin:0))return false;
    /* The current rotating label is always painted in the target colours. */
    unsigned slot=art_theme(theme)-1;
    const adaptive_asset_t *label=&adaptive_static_label[slot*ADAPTIVE_LABELS+(samples%25600)*ADAPTIVE_LABELS/25600];
    if(!decode(s,label))return false;
    const uint16_t *pixels=(const uint16_t*)s->scratch;
    /* The rotating paper contains long runs of the same small RGB565 ramp.
     * This exact frame-local cache never retains an old theme palette. */
    uint32_t tints[128];uint8_t tint_valid[16]={0};unsigned cache_hits=0,cache_misses=0;
    for(unsigned row=0;row<label->h;row++){
        unsigned y=label->y+row,prepared_tile=UINT_MAX;
        uint16_t *out=s->canvas+y*s->stride+label->x;
        for(unsigned col=0;col<label->w;col++){
            unsigned i=row*label->w+col;
            if(!(adaptive_label_mask[i>>3]&(1u<<(i&7))))continue;
            uint16_t raw=pixels[i],colour;
            if(spindle(col,row))colour=raw;
            else{
                unsigned slot=(raw^(raw>>7)^(raw>>11))&127,mask=1u<<(slot&7);
                if((tint_valid[slot>>3]&mask)&&(uint16_t)(tints[slot]>>16)==raw)
                    {colour=(uint16_t)tints[slot];cache_hits++;}
                else{
                    cache_misses++;
                    colour=paper_colour(s,raw);
                    tints[slot]=((uint32_t)raw<<16)|colour;tint_valid[slot>>3]|=(uint8_t)mask;
                }
            }
            unsigned x=label->x+col,tile=y/32*15+x/32;
            if(tile!=prepared_tile){prepare(s,x,y);prepared_tile=tile;}
            out[col]=colour;
        }
    }
    s->profile.paper_cache_hits+=cache_hits;s->profile.paper_cache_misses+=cache_misses;
    return true;
}
static void disable_transition(adaptive_scene_t *s){
    s->transition_background=NULL;s->transition_duration_ms=0;s->transition_start_ms=0;
    s->transition_active=false;s->transition_alpha=255;s->transition_count=0;s->transition_source_theme=0;
}
bool adaptive_scene_transition_init(adaptive_scene_t *s,uint16_t *snapshot,size_t count,unsigned duration_ms){
    (void)snapshot;(void)count;(void)duration_ms;
    if(!s)return false;
    disable_transition(s);s->transition_now_ms=0;s->transition_automatic=false;return true;
}
void adaptive_scene_transition_clock(adaptive_scene_t *s,uint32_t now_ms,bool automatic){
    if(!s)return;
    disable_transition(s);s->transition_now_ms=now_ms;s->transition_automatic=automatic;
}
static bool scratch_range(const void *pointer,size_t bytes,uintptr_t *end){
    uintptr_t start=(uintptr_t)pointer;
    if(!pointer||!bytes||bytes>UINTPTR_MAX-start)return false;
    *end=start+bytes;return true;
}
static bool scratch_disjoint(const void *a,size_t a_bytes,const void *b,size_t b_bytes){
    uintptr_t a_end,b_end;
    return scratch_range(a,a_bytes,&a_end)&&scratch_range(b,b_bytes,&b_end)
        &&(a_end<=(uintptr_t)b||b_end<=(uintptr_t)a);
}
bool adaptive_scene_configure_scratch(adaptive_scene_t *s,uint8_t *main_scratch,
                                      size_t main_bytes,uint8_t *neon_fallback,
                                      size_t neon_bytes){
    if(!s||s->scratch_partitioned||s->neon_begin||s->neon_collect
       ||!main_scratch||main_scratch!=s->scratch
       ||main_bytes<SCENE_MAIN_SCRATCH_BYTES||main_bytes>s->main_capacity
       ||neon_bytes<SCENE_SCRATCH_BYTES
       ||((uintptr_t)main_scratch&127u)||((uintptr_t)neon_fallback&127u)
       ||(main_bytes&127u)||(neon_bytes&127u)
       ||!scratch_disjoint(main_scratch,s->main_capacity,neon_fallback,neon_bytes))return false;
    /* Output and decoded art must never alias the full fallback workspace. */
    size_t canvas_row=(size_t)s->stride*sizeof(uint16_t);
    if(canvas_row/sizeof(uint16_t)!=s->stride||canvas_row>SIZE_MAX/SCENE_HEIGHT
       ||!scratch_disjoint(neon_fallback,neon_bytes,s->canvas,canvas_row*SCENE_HEIGHT)
       ||!scratch_disjoint(neon_fallback,neon_bytes,s->background,SCENE_WIDTH*SCENE_HEIGHT*sizeof(uint16_t))
       ||!scratch_disjoint(neon_fallback,neon_bytes,s->trail_alpha,SCENE_WIDTH*SCENE_HEIGHT))return false;
    s->scratch=main_scratch;s->main_capacity=main_bytes;
    s->neon_scratch=neon_fallback;s->neon_capacity=neon_bytes;s->scratch_partitioned=true;
    return true;
}
bool adaptive_scene_init(adaptive_scene_t *s,uint16_t *canvas,unsigned stride,uint16_t *background,uint8_t *scratch,size_t bytes,uint8_t *trail_alpha){
    if(!s||!canvas||!background||!scratch||!trail_alpha||stride<480||bytes<SCENE_SCRATCH_BYTES)return false;
    memset(s,0,sizeof(*s));s->ambience_enabled=true;s->canvas=canvas;s->stride=stride;s->background=background;s->scratch=scratch;s->neon_scratch=scratch;s->main_capacity=SCENE_SCRATCH_BYTES;s->neon_capacity=SCENE_SCRATCH_BYTES;s->trail_alpha=trail_alpha;s->last_samples=UINT64_MAX;
    adaptive_scene_motion_cache_reset(s);label_opaque_init(s);disable_transition(s);
    for(unsigned y=0;y<SCENE_HEIGHT;y++){
        unsigned first=SCENE_WIDTH,last=0;
        for(unsigned x=0;x<SCENE_WIDTH;x++){
            float dx=x+.5f-195.56f,dy=y+.5f-303.36f;
            float u=.68847183f*dx-.37187559f*dy,v=.40311718f*dx+1.16611083f*dy;
            if(u*u+v*v<224.0f*224.0f){if(first==SCENE_WIDTH)first=x;last=x+1;}
        }
        s->scenery_inside[y][0]=(uint16_t)first;s->scenery_inside[y][1]=(uint16_t)last;
    }
    for(unsigned j=0;j<SCENE_TRAIL_POINTS;j++){
        float fresh=(float)j/(SCENE_TRAIL_POINTS-1);
        s->trail_fresh[j]=fresh;s->trail_taper[j]=.53f+.47f*smoothf(fresh);
        s->trail_fade[j]=225*powf(fresh,1.4f);
    }
    return background_theme(s,1,0);
}
bool SCENE_IRAM_ATTR adaptive_scene_render(adaptive_scene_t *s,const motion_timeline_t *timeline,uint64_t samples,unsigned skin,unsigned song,bool playing){
    if(!s||!timeline||skin>=SCENE_SKINS||timeline->theme>=SONG_THEME_COUNT||timeline->record_id>=RECORD_COLOR_COUNT||timeline->effect_id>=SCENE_EFFECTS)return false;
    uint32_t started=scene_now_us(),phase_start=started;
    memset(&s->profile,0,sizeof(s->profile));disable_transition(s);
    motion_cache_bind(s,timeline,samples);
    unsigned theme=canonical_theme(timeline->theme);
    memset(s->dirty,0,sizeof(s->dirty));
    bool changed_theme=theme!=s->last_theme||timeline->record_id!=s->last_record_id;
    bool copied_environment=false;
    if(!changed_theme&&samples==s->last_samples&&skin==s->last_skin&&song==s->last_song&&playing==s->last_playing&&timeline->effect_id==s->last_effect_id&&s->ambience_enabled==s->last_ambience_enabled)return true;
    if(changed_theme){
        if(!background_theme(s,theme,timeline->record_id))return false;
    }else{
        memset(s->prepared,0,sizeof(s->prepared));
        copied_environment=restore_occupied(s);
        memset(s->occupied,0,sizeof(s->occupied));
    }
    s->profile.environment_us=scene_now_us()-phase_start;phase_start=scene_now_us();
    s->profile.motion_evaluations++;
    motion_pose_t q;if(!motion_evaluate(timeline,samples,&q))return false;
    unsigned neon_first=0,neon_second=0;float neon_blend=0;
    uint32_t neon_token=0;bool neon_pending=false;
    bool live_character=skin==ADAPTIVE_SKINS;
#if SCENE_PC_SMOOTH_CHARACTERS
    live_character=true;
#endif
    if(live_character){
        const adaptive_clip_t *clip=&adaptive_clips[q.clip];
        float position=clampf(q.phase,0,1)*(clip->count-1);
        unsigned first=(unsigned)floorf(position),second=first+1<clip->count?first+1:first;
        neon_first=clip->first+first;neon_second=clip->first+second;neon_blend=position-first;
#if !SCENE_PC_SMOOTH_CHARACTERS
        if(s->neon_begin&&s->neon_collect)
            neon_pending=s->neon_begin(s->neon_context,neon_first,neon_second,neon_blend,
                                        SCENE_CHARACTER_SCALE,&neon_token);
#endif
    }
    if(!draw_record_details(s,theme,samples,skin))return false;
    s->profile.details_us=scene_now_us()-phase_start;phase_start=scene_now_us();
    s->ambience_pixels=0;
    if(s->ambience_enabled)scene_ambience_draw_clipped(theme,samples,scenery_plot,s,s->scenery_inside);
    s->profile.ambience_us=scene_now_us()-phase_start;phase_start=scene_now_us();
    if(!trails(s,timeline,samples,skin))return false;
    s->profile.trails_us=scene_now_us()-phase_start;phase_start=scene_now_us();
    s->effect_pixels=s->effect_back_pixels=s->effect_front_pixels=0;
    s->effect_mode=foot_effect_resolve(timeline->effect_id,skin);
    foot_effect_context_t fx;if(!effect_context(s,timeline,samples,&q,skin,&fx))return false;
    effect_plot_context_t effect_plot={.scene=s,.layer=0};
    if(!landings(s,timeline,samples,&fx,&effect_plot))return false;
    foot_effect_draw(&fx,s->effect_mode,0,effect_pixel,&effect_plot);
    s->profile.effects_us=scene_now_us()-phase_start;phase_start=scene_now_us();
    unsigned frame=sprite_frame(&q);if(frame>=ADAPTIVE_SPRITES)return false;
    float x,y;project(q.u,q.v,0,&x,&y);
    if(!live_character){
        const adaptive_asset_t *a=&adaptive_sprites[frame];
        if(!character_sprite(s,a,(int)lroundf(x),(int)lroundf(y),skin))return false;
    }else{
        adaptive_scene_neon_result_t result={0};
        bool collected=neon_pending&&s->neon_collect(s->neon_context,neon_token,&result);
        /* Reject malformed native output before any pixel read. Current mesh
         * bounds fit comfortably; the division also prevents size overflow. */
        if(!collected||!result.pixels||!result.width||!result.height
                ||result.width>SCENE_SCRATCH_BYTES/3
                ||result.height>SCENE_SCRATCH_BYTES/3/result.width
                ||result.left<INT_MIN/2||result.left>INT_MAX/2
                ||result.top<INT_MIN/2||result.top>INT_MAX/2){
#if SCENE_PC_SMOOTH_CHARACTERS
            skating_secondary_motion_t secondary;
            if(!adaptive_scene_secondary_motion(timeline,samples,&secondary))return false;
            s->profile.motion_evaluations+=CHARACTER_HISTORY_COUNT;
            if(!skating_character_draw_with_secondary(neon_first,neon_second,neon_blend,
                  SCENE_CHARACTER_SCALE,skin,x-lroundf(x),y-lroundf(y),&secondary,
                  s->neon_scratch,s->neon_capacity,
                  &result.left,&result.top,&result.width,&result.height))return false;
#else
            if(!neon_character_draw_interpolated(neon_first,neon_second,neon_blend,
                  SCENE_CHARACTER_SCALE,s->neon_scratch,s->neon_capacity,
                  &result.left,&result.top,&result.width,&result.height))return false;
#endif
            result.pixels=s->neon_scratch;
        }
        int left=result.left,top=result.top;unsigned width=result.width,height=result.height;
        int x0=(int)lroundf(x)+left,y0=(int)lroundf(y)+top;
        unsigned first_row=y0<0?(unsigned)-y0:0,last_row=height;
        unsigned first_col=x0<0?(unsigned)-x0:0,last_col=width;
        if(y0+(int)last_row>480)last_row=(unsigned)(480-y0);
        if(x0+(int)last_col>480)last_col=(unsigned)(480-x0);
        unsigned last_tile=UINT_MAX;
        if(x0<480&&y0<480&&first_col<last_col&&first_row<last_row)
        for(unsigned row=first_row;row<last_row;row++)for(unsigned col=first_col;col<last_col;col++){
            const uint8_t *p=result.pixels+(row*width+col)*3;
            character_pixel(s,x0+(int)col,y0+(int)row,
                  (uint16_t)(p[0]|((unsigned)p[1]<<8)),p[2],&last_tile);
        }
    }
    s->profile.character_us=scene_now_us()-phase_start;phase_start=scene_now_us();
    effect_plot.layer=1;foot_effect_draw(&fx,s->effect_mode,1,effect_pixel,&effect_plot);
    s->profile.effects_us+=scene_now_us()-phase_start;phase_start=scene_now_us();
    if(!draw_control(s,theme,playing))return false;
    s->profile.controls_us=scene_now_us()-phase_start;
    /* Full restore prepared even untouched tiles. All drawing paths record
     * their actual ink in occupied, including direct label/trail row writes. */
    if(copied_environment)for(unsigned i=0;i<SCENE_TILES;i++)s->dirty[i]|=s->occupied[i];
    s->last_ambience_enabled=s->ambience_enabled;
    s->last_samples=samples;s->last_skin=skin;s->last_song=song;s->last_playing=playing;s->last_effect_id=timeline->effect_id;
    s->clip=q.clip;s->phase=q.phase;s->sprite=frame;
    s->profile.total_us=scene_now_us()-started;
    return true;
}

void adaptive_scene_set_environment_blend(adaptive_scene_t *s,
                                         adaptive_scene_environment_blend_fn blend,void *context){
    if(!s)return;
    s->environment_blend=blend;s->environment_blend_context=context;
}

void adaptive_scene_set_environment_copy(adaptive_scene_t *s,
                                        adaptive_scene_environment_copy_fn copy,void *context){
    if(!s)return;
    s->environment_copy=copy;s->environment_copy_context=context;
}

void adaptive_scene_set_dense_copy(adaptive_scene_t *s,bool enabled){
    if(s)s->dense_copy_enabled=enabled;
}

void adaptive_scene_set_neon_async(adaptive_scene_t *s,adaptive_scene_neon_begin_fn begin,
                                  adaptive_scene_neon_collect_fn collect,void *context){
    if(!s)return;
    s->neon_begin=begin;s->neon_collect=collect;s->neon_context=context;
}
