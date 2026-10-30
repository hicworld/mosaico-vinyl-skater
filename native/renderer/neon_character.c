/* Compact articulated Neon fan character. This is a mesh character, not an
 * alternate material palette: the tied hair, cropped vest, bare arms, gloves,
 * trouser stripes and skate uppers have their own geometry. The source rig is
 * the same solved skeleton as the existing motion bank. */
#include "neon_character.h"
#include "neon_rig.h"
#include <math.h>
#include <string.h>
#include <limits.h>

/* Native hot code executes in internal SRAM; the desktop renderer keeps
 * the same functions and arithmetic with an empty placement attribute. */
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define NEON_IRAM_ATTR IRAM_ATTR
#else
#define NEON_IRAM_ATTR
#endif

#define NEON_PI 3.14159265358979323846f
#define NEON_SCALE 2.7f
#define NEON_POINT_SCALE (1.0f/64.0f)
#define NEON_AXIS_SCALE (1.0f/127.0f)

typedef struct {float x,y,z;} vec_t;
typedef struct {uint8_t r,g,b;} colour_t;
typedef struct {
    int x,y; unsigned w,h,sw,sh;
    int left,top,right,bottom;
    float scale,anchor_x,anchor_y;
    unsigned skin;
#if SCENE_PC_SMOOTH_CHARACTERS
    bool secondary;
    skating_secondary_motion_t motion;
#endif
    bool measuring;
    uint8_t *prepared_end;
    size_t prepared_used,prepared_count,prepared_capacity;
    bool prepared_overflow;
    uint16_t *colour; int16_t *depth; uint8_t *alpha;
    vec_t p[14],basis[4][3];
    /* Angle samples are shared by every limb and wheel in this one draw.
     * Local storage keeps seeking and concurrent renderer instances isolated. */
    float circle_sin[4][13],circle_cos[4][13];
    float latitude_sin[5][9],latitude_cos[5][9];
    unsigned circle_ready,latitude_ready;
} draw_t;

static const colour_t skin={206,145,109},skin_light={228,169,126};
static const colour_t navy={30,43,87},navy_light={43,62,112};
static const colour_t blue={34,107,222},blue_light={54,162,248};
static const colour_t hair_blue={44,76,219},hair_light={66,123,247};
static const colour_t lime={214,243,70},white={218,237,246};
static const colour_t rubber={24,35,53},metal={128,158,177};
static vec_t add(vec_t a,vec_t b){return (vec_t){a.x+b.x,a.y+b.y,a.z+b.z};}
static vec_t sub(vec_t a,vec_t b){return (vec_t){a.x-b.x,a.y-b.y,a.z-b.z};}
static vec_t mul(vec_t a,float b){return (vec_t){a.x*b,a.y*b,a.z*b};}
static float dot(vec_t a,vec_t b){return a.x*b.x+a.y*b.y+a.z*b.z;}
static vec_t cross(vec_t a,vec_t b){return (vec_t){a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x};}
static vec_t unit(vec_t a){float n=sqrtf(dot(a,a));return n>1e-7f?mul(a,1.0f/n):(vec_t){0,0,1};}
static vec_t mix(vec_t a,vec_t b,float t){return add(a,mul(sub(b,a),t));}
static float minimum(float a,float b){return a<b?a:b;}
static float maximum(float a,float b){return a>b?a:b;}
static vec_t local(vec_t origin,const vec_t *basis,float x,float y,float z){
    return add(origin,mul(add(mul(basis[0],x),add(mul(basis[1],y),mul(basis[2],z))),NEON_SCALE));
}

/* Same camera as generate_adaptive_art.actor_project, without its anchor. */
static vec_t project(const draw_t *d,vec_t p){
    const float ca=.9179624460f,sa=-.3966673010f;
    return (vec_t){d->anchor_x+d->scale*(ca*p.x-sa*.738f*p.y)*(4.0f/3.0f),
                   d->anchor_y+d->scale*((sa*p.x+ca*.738f*p.y)*(16.0f/15.0f)-p.z*(16.0f/15.0f)),
                   d->scale*(-.2412025941f*p.x+.7563522893f*p.y+.6080727942f*p.z)};
}
static uint16_t packed_colour(colour_t c,float shade){
    unsigned r=(unsigned)(c.r*shade),g=(unsigned)(c.g*shade),b=(unsigned)(c.b*shade);
    return (uint16_t)(((r>>3)<<11)|((g>>2)<<5)|(b>>3));
}
static float edge(vec_t a,vec_t b,float x,float y){return (b.x-a.x)*(y-a.y)-(b.y-a.y)*(x-a.x);}
static void NEON_IRAM_ATTR triangle(draw_t *d,vec_t a,vec_t b,vec_t c,uint16_t colour){
    float area=edge(a,b,c.x,c.y);
    if(fabsf(area)<1e-7f)return;
    if(area<0){vec_t t=b;b=c;c=t;area=-area;}
    int x0=(int)floorf(minimum(a.x,minimum(b.x,c.x)));
    int y0=(int)floorf(minimum(a.y,minimum(b.y,c.y)));
    int x1=(int)ceilf(maximum(a.x,maximum(b.x,c.x)));
    int y1=(int)ceilf(maximum(a.y,maximum(b.y,c.y)));
    if(d->measuring){
        if(x0<d->left)d->left=x0;
        if(y0<d->top)d->top=y0;
        if(x1>d->right)d->right=x1;
        if(y1>d->bottom)d->bottom=y1;
        return;
    }
    /* Pixel centres on a 2x grid, orthographic depth is affine. */
    a.x=(a.x-d->x)*2;a.y=(a.y-d->y)*2;
    b.x=(b.x-d->x)*2;b.y=(b.y-d->y)*2;
    c.x=(c.x-d->x)*2;c.y=(c.y-d->y)*2;
    x0=(x0-d->x)*2;y0=(y0-d->y)*2;
    x1=(x1-d->x)*2;y1=(y1-d->y)*2;
    if(x0<0)x0=0;
    if(y0<0)y0=0;
    if(x1>(int)d->sw)x1=(int)d->sw;
    if(y1>(int)d->sh)y1=(int)d->sh;
    float inv=1.0f/(area*4),dzx=((b.y-c.y)*a.z+(c.y-a.y)*b.z+(a.y-b.y)*c.z)*inv;
    float dzy=((c.x-b.x)*a.z+(a.x-c.x)*b.z+(b.x-a.x)*c.z)*inv;
    float wa=edge(b,c,x0+.5f,y0+.5f),wb=edge(c,a,x0+.5f,y0+.5f),wc=edge(a,b,x0+.5f,y0+.5f);
    float depth=(wa*a.z+wb*b.z+wc*c.z)*inv;
    float ax=b.y-c.y,bx=c.y-a.y,cx=a.y-b.y;
    float ay=c.x-b.x,by=a.x-c.x,cy=b.x-a.x;
    for(int y=y0;y<y1;y++){
        float ea=wa,eb=wb,ec=wc,zz=depth;
        size_t index=(size_t)y*d->sw+(unsigned)x0;
        for(int x=x0;x<x1;x++,index++){
            if(ea>=-.0001f&&eb>=-.0001f&&ec>=-.0001f){
                int zi=(int)lroundf(zz*128);
                if(zi>INT16_MAX)zi=INT16_MAX;
                if(zi<INT16_MIN+1)zi=INT16_MIN+1;
                if(zi>=d->depth[index]){d->depth[index]=(int16_t)zi;d->colour[index]=colour;d->alpha[index]=255;}
            }
            ea+=ax;eb+=bx;ec+=cx;zz+=dzx;
        }
        wa+=ay;wb+=by;wc+=cy;depth+=dzy;
    }
}
static void NEON_IRAM_ATTR face(draw_t *d,const vec_t *vertices,unsigned count,colour_t colour){
    vec_t normal=unit(cross(sub(vertices[1],vertices[0]),sub(vertices[2],vertices[0])));
    const vec_t view={-.2412025941f,.7563522893f,.6080727942f};
    if(dot(normal,view)<-.005f)return;
    const vec_t light={-.5050763f,-.3030458f,.8081220f};
    float lit=dot(normal,light);if(lit<0)lit=0;
    uint16_t pixel=packed_colour(colour,.69f+.31f*lit);
    vec_t *prepared_vertices=NULL;
    if(d->measuring&&!d->prepared_overflow){
        size_t bytes=sizeof(uint32_t)+count*sizeof(vec_t);
        if(bytes<=d->prepared_capacity-d->prepared_used){
            uint8_t *record=d->prepared_end-d->prepared_used-bytes;
            prepared_vertices=(vec_t*)(void*)record;
            *(uint32_t*)(void*)(record+count*sizeof(vec_t))=(count<<16)|pixel;
            d->prepared_used+=bytes;d->prepared_count++;
        }else d->prepared_overflow=true;
    }
    vec_t a=project(d,vertices[0]),b=project(d,vertices[1]);
    if(prepared_vertices){prepared_vertices[0]=a;prepared_vertices[1]=b;}
    for(unsigned i=2;i<count;i++){
        vec_t c=project(d,vertices[i]);
        if(prepared_vertices)prepared_vertices[i]=c;
        triangle(d,a,b,c,pixel);b=c;
    }
}
static void quad(draw_t *d,vec_t a,vec_t b,vec_t c,vec_t e,colour_t colour){vec_t q[]={a,b,c,e};face(d,q,4,colour);}

static unsigned circle_angles(draw_t *d,unsigned segments){
    unsigned slot=segments==4?0:(segments==6?1:(segments==8?2:3)),mask=1u<<slot;
    if(!(d->circle_ready&mask)){
        for(unsigned i=0;i<=segments;i++){
            float angle=2*NEON_PI*i/segments;
            d->circle_sin[slot][i]=sinf(angle);d->circle_cos[slot][i]=cosf(angle);
        }
        d->circle_ready|=mask;
    }
    return slot;
}
static unsigned latitude_angles(draw_t *d,unsigned parallels){
    unsigned slot=parallels==2?0:(parallels==3?1:(parallels==4?2:(parallels==6?3:4))),mask=1u<<slot;
    if(!(d->latitude_ready&mask)){
        for(unsigned i=0;i<=parallels;i++){
            float angle=NEON_PI*i/parallels;
            d->latitude_sin[slot][i]=sinf(angle);d->latitude_cos[slot][i]=cosf(angle);
        }
        d->latitude_ready|=mask;
    }
    return slot;
}
static void NEON_IRAM_ATTR tube(draw_t *d,vec_t a,vec_t b,float r0,float r1,colour_t colour,unsigned segments){
    vec_t axis=unit(sub(b,a));
    vec_t helper=fabsf(axis.z)<.9f?(vec_t){0,0,1}:(vec_t){1,0,0};
    vec_t ex=unit(cross(helper,axis)),ey=cross(axis,ex),ring[2][12];
    unsigned angles=circle_angles(d,segments);
    for(unsigned i=0;i<segments;i++){
        vec_t radial=add(mul(ex,d->circle_cos[angles][i]),mul(ey,d->circle_sin[angles][i]));
        ring[0][i]=add(a,mul(radial,r0*NEON_SCALE));ring[1][i]=add(b,mul(radial,r1*NEON_SCALE));
    }
    for(unsigned i=0;i<segments;i++){unsigned j=(i+1)%segments;quad(d,ring[0][i],ring[0][j],ring[1][j],ring[1][i],colour);}
    vec_t rev[12];for(unsigned i=0;i<segments;i++)rev[i]=ring[0][segments-i-1];
    face(d,rev,segments,colour);face(d,ring[1],segments,colour);
}
static void ellipsoid_ring(vec_t *ring,vec_t centre,vec_t radius,const vec_t *basis,
                            unsigned meridians,float sp,float cp,const float *st,const float *ct){
    for(unsigned i=0;i<=meridians;i++)
        ring[i]=local(centre,basis,radius.x*sp*ct[i],radius.y*sp*st[i],radius.z*cp);
}
static void NEON_IRAM_ATTR ellipsoid(draw_t *d,vec_t centre,vec_t radius,const vec_t *basis,colour_t colour,
                      unsigned meridians,unsigned parallels,bool hair_cap){
    /* Adjacent faces share their exact vertices. Preserve the terminal 2pi
     * sample (rather than wrapping to zero), the original face order and all
     * float expression order while computing each mesh ring just once. */
    vec_t ring[2][13];
    unsigned angles=circle_angles(d,meridians),latitudes=latitude_angles(d,parallels);
    const float *st=d->circle_sin[angles],*ct=d->circle_cos[angles];
    const float *sp=d->latitude_sin[latitudes],*cp=d->latitude_cos[latitudes];
    ellipsoid_ring(ring[0],centre,radius,basis,meridians,sp[0],cp[0],st,ct);
    for(unsigned j=0;j<parallels;j++){
        float p0=NEON_PI*j/parallels,p1=NEON_PI*(j+1)/parallels;
        vec_t *lower=ring[j&1],*upper=ring[(j+1)&1];
        ellipsoid_ring(upper,centre,radius,basis,meridians,sp[j+1],cp[j+1],st,ct);
        for(unsigned i=0;i<meridians;i++){
            float t0=2*NEON_PI*i/meridians,t1=2*NEON_PI*(i+1)/meridians;
            if(hair_cap&&(p0+p1)*.5f>=NEON_PI*.42f&&sinf((t0+t1)*.5f)>=-.10f)continue;
            vec_t vertices[]={lower[i],upper[i],upper[i+1],lower[i+1]};
            /* Pole quads have two coincident vertices; choose a valid normal. */
            if(j==0){vec_t tri[]={vertices[0],vertices[1],vertices[2]};face(d,tri,3,colour);}
            else if(j==parallels-1){vec_t tri[]={vertices[0],vertices[1],vertices[3]};face(d,tri,3,colour);}
            else face(d,vertices,4,colour);
        }
    }
}
static void box(draw_t *d,vec_t centre,vec_t half,const vec_t *basis,colour_t colour){
    static const int8_t signs[8][3]={{-1,-1,-1},{1,-1,-1},{1,1,-1},{-1,1,-1},{-1,-1,1},{1,-1,1},{1,1,1},{-1,1,1}};
    static const uint8_t ids[6][4]={{3,2,1,0},{4,5,6,7},{0,1,5,4},{1,2,6,5},{2,3,7,6},{3,0,4,7}};
    vec_t p[8];for(unsigned i=0;i<8;i++)p[i]=local(centre,basis,signs[i][0]*half.x,signs[i][1]*half.y,signs[i][2]*half.z);
    for(unsigned i=0;i<6;i++)quad(d,p[ids[i][0]],p[ids[i][1]],p[ids[i][2]],p[ids[i][3]],colour);
}

static void shadow(draw_t *d,float cx,float cy,float rx,float ry,unsigned alpha){
    if(alpha<2)return;
    int x0=(int)floorf(cx-rx),x1=(int)ceilf(cx+rx),y0=(int)floorf(cy-ry),y1=(int)ceilf(cy+ry);
    if(d->measuring){
        if(x0<d->left)d->left=x0;
        if(y0<d->top)d->top=y0;
        if(x1>d->right)d->right=x1;
        if(y1>d->bottom)d->bottom=y1;
        return;
    }
    x0=(x0-d->x)*2;y0=(y0-d->y)*2;x1=(x1-d->x)*2;y1=(y1-d->y)*2;
    if(x0<0)x0=0;
    if(y0<0)y0=0;
    if(x1>(int)d->sw)x1=(int)d->sw;
    if(y1>(int)d->sh)y1=(int)d->sh;
    for(int y=y0;y<y1;y++)for(int x=x0;x<x1;x++){
        float dx=(d->x+(x+.5f)*.5f-cx)/rx,dy=(d->y+(y+.5f)*.5f-cy)/ry,r=dx*dx+dy*dy;
        if(r>=1)continue;
        unsigned a=(unsigned)(alpha*(1-r)*(1-r));size_t i=(size_t)y*d->sw+(unsigned)x;
        if(a>d->alpha[i]){d->alpha[i]=(uint8_t)a;d->colour[i]=0x0021;}
    }
}
static float skate_floor_height(draw_t *d,unsigned side){
    const vec_t *b=d->basis[2+side];
    float radius=sqrtf(.36f*.36f*b[0].z*b[0].z+.61f*.61f*b[1].z*b[1].z+.58f*.58f*b[2].z*b[2].z);
    return maximum(0,d->p[12+side].z+NEON_SCALE*(.57f*b[2].z-.92f*fabsf(b[0].z)+minimum(-.75f*b[1].z,2.10f*b[1].z)-radius));
}
static void draw_shadows(draw_t *d){
    for(unsigned side=0;side<2;side++){
        vec_t p=d->p[12+side],screen=project(d,(vec_t){p.x,p.y,0});
        float height=skate_floor_height(d,side),weight=expf(-height/(NEON_SCALE*2.2f));
        shadow(d,screen.x+1.3f*d->scale,screen.y+.6f*d->scale,
               (5.2f+height*.12f)*d->scale,(2.7f+height*.07f)*d->scale,(unsigned)(105*weight));
    }
    vec_t centre=mul(add(d->p[12],d->p[13]),.5f),screen=project(d,(vec_t){centre.x,centre.y,0});
    float height=minimum(skate_floor_height(d,0),skate_floor_height(d,1));
    shadow(d,screen.x+5.5f*d->scale,screen.y+1.4f*d->scale,
           (15.0f+height*.11f)*d->scale,(5.8f+height*.06f)*d->scale,(unsigned)(46*expf(-height/10)));
}

#if SCENE_PC_SMOOTH_CHARACTERS
static float secondary_limit(float value,float limit){
    return minimum(limit,maximum(-limit,value));
}
/* Attachment displacement uses unscaled model units; twist uses radians.
 * Roots have zero weight, and the solved skeleton is never changed. */
static vec_t secondary_hair_point(const draw_t *d,vec_t root,vec_t point,
                                  const vec_t *b,float weight,float side,float strength){
    if(!d->secondary)return point;
    const skating_secondary_motion_t *m=&d->motion;
    vec_t delta=mul(sub(point,root),1.0f/NEON_SCALE);
    float x=dot(delta,b[0]),y=dot(delta,b[1]);
    float angle=m->twist*weight*strength,c=cosf(angle),s=sinf(angle);
    float amount=weight*strength;
    return local(point,b,(x*c-y*s-x)+(m->hair_side+side*m->hair_flare)*amount,
                 (x*s+y*c-y)+(m->hair_back-m->hair_flare*.20f)*amount,
                 (m->hair_lift+m->hair_flare*.15f)*amount);
}
static void secondary_jacket_panel(draw_t *d,vec_t hip,const vec_t *b,float side,colour_t colour){
    vec_t centre=local(hip,b,side*1.42f,-.05f,.3f);
    if(!d->secondary){box(d,centre,(vec_t){.62f,1.52f,1.10f},b,colour);return;}
    static const int8_t signs[8][3]={{-1,-1,-1},{1,-1,-1},{1,1,-1},{-1,1,-1},{-1,-1,1},{1,-1,1},{1,1,1},{-1,1,1}};
    static const uint8_t ids[6][4]={{3,2,1,0},{4,5,6,7},{0,1,5,4},{1,2,6,5},{2,3,7,6},{3,0,4,7}};
    const skating_secondary_motion_t *m=&d->motion;vec_t p[8];
    for(unsigned i=0;i<8;i++){
        float weight=signs[i][2]<0?1.0f:0.0f;
        p[i]=local(centre,b,signs[i][0]*.62f+weight*(m->jacket_sway*.35f+side*m->hem_flare*.18f),
                   signs[i][1]*1.52f+weight*m->hem_back*.33f,
                   signs[i][2]*1.10f+weight*m->hem_lift*.17f);
    }
    for(unsigned i=0;i<6;i++)quad(d,p[ids[i][0]],p[ids[i][1]],p[ids[i][2]],p[ids[i][3]],colour);
}
#endif

static void NEON_IRAM_ATTR clothes(draw_t *d){
    vec_t hip=mul(add(d->p[0],d->p[1]),.5f),shoulder=mul(add(d->p[4],d->p[5]),.5f);
    vec_t *basis=d->basis[0];
    ellipsoid(d,hip,(vec_t){2.2f,1.7f,1.4f},basis,navy,8,4,false);
    ellipsoid(d,mix(hip,shoulder,.24f),(vec_t){1.94f,1.43f,2.1f},basis,skin,8,4,false);
    const float sections[8][2]={{-1,-.62f},{-.68f,-1},{.68f,-1},{1,-.62f},{1,.62f},{.68f,1},{-.68f,1},{-1,.62f}};
    vec_t rings[3][8];float widths[]={2.16f,2.65f,2.32f},depths[]={1.49f,1.64f,1.30f},heights[]={.24f,.91f,1.03f};
    for(unsigned k=0;k<3;k++)for(unsigned i=0;i<8;i++)rings[k][i]=local(mix(hip,shoulder,heights[k]),basis,sections[i][0]*widths[k],sections[i][1]*depths[k],0);
    for(unsigned k=0;k<2;k++)for(unsigned i=0;i<8;i++){
        unsigned j=(i+1)%8;colour_t c=(i==4||i==5)?white:blue;
        quad(d,rings[k][i],rings[k][j],rings[k+1][j],rings[k+1][i],c);
    }
    face(d,rings[2],8,blue);vec_t cap[8];for(unsigned i=0;i<8;i++)cap[i]=rings[0][7-i];face(d,cap,8,navy);
    /* Broad asymmetric chest panels and a small fluorescent zipper remain
     * legible on the actual display without adding outlines to the limbs. */
    vec_t low=mix(hip,shoulder,.34f),high=mix(hip,shoulder,.90f);
    quad(d,local(low,basis,-.45f,1.59f,0),local(low,basis,-1.58f,1.54f,0),
           local(high,basis,-1.65f,1.66f,0),local(high,basis,.53f,1.66f,0),blue_light);
    quad(d,local(low,basis,.90f,1.57f,0),local(low,basis,.68f,1.57f,0),
           local(high,basis,.68f,1.68f,0),local(high,basis,.90f,1.68f,0),lime);
    /* Back panel is separate from the front, so turnarounds reveal detail. */
    quad(d,local(low,basis,-1.1f,-1.57f,0),local(low,basis,1.1f,-1.57f,0),
           local(high,basis,1.65f,-1.68f,0),local(high,basis,-1.65f,-1.68f,0),navy_light);
    quad(d,local(mix(hip,shoulder,.54f),basis,-.86f,-1.65f,0),local(mix(hip,shoulder,.44f),basis,-.05f,-1.63f,0),
           local(mix(hip,shoulder,.71f),basis,.48f,-1.68f,0),local(mix(hip,shoulder,.80f),basis,-.34f,-1.69f,0),lime);
    for(unsigned j=0;j<2;j++){
        float sign=j?1.0f:-1.0f;vec_t knee=d->p[2+j];
        vec_t ankle=local(d->p[12+j],d->basis[2+j],0,-.15f,2.69f);
        tube(d,d->p[j],knee,1.30f,1.17f,navy,6);
        ellipsoid(d,knee,(vec_t){1.20f,1.20f,1.12f},basis,navy_light,6,3,false);
        tube(d,knee,ankle,1.14f,.76f,navy,6);
        vec_t stripe0=add(mix(d->p[j],knee,.15f),mul(basis[0],sign*1.20f*NEON_SCALE));
        vec_t stripe1=add(mix(d->p[j],knee,.84f),mul(basis[0],sign*1.10f*NEON_SCALE));
        tube(d,stripe0,stripe1,.145f,.145f,j?blue_light:lime,6);
        vec_t shoulder0=d->p[4+j],elbow=d->p[6+j],wrist=d->p[8+j];
        tube(d,shoulder0,elbow,.77f,.66f,skin,6);
        ellipsoid(d,elbow,(vec_t){.69f,.69f,.69f},basis,skin_light,6,3,false);
        tube(d,elbow,wrist,.65f,.54f,skin,6);
        if(j==0)tube(d,mix(shoulder0,elbow,.14f),mix(shoulder0,elbow,.35f),.81f,.78f,blue,6);
        tube(d,mix(elbow,wrist,.74f),mix(elbow,wrist,.89f),.62f,.60f,j?blue_light:lime,6);
        tube(d,mix(elbow,wrist,.88f),mix(elbow,wrist,.96f),.64f,.62f,navy_light,6);
        vec_t hand_axis=unit(sub(wrist,elbow));
        vec_t hand_side=sub(basis[0],mul(hand_axis,dot(basis[0],hand_axis)));
        if(dot(hand_side,hand_side)<1e-8f)hand_side=cross(basis[1],hand_axis);
        hand_side=unit(hand_side);
        vec_t hand_basis[]={hand_side,unit(cross(hand_axis,hand_side)),hand_axis};
        ellipsoid(d,add(wrist,mul(hand_axis,.23f*NEON_SCALE)),(vec_t){.60f,.48f,.74f},hand_basis,navy,6,3,false);
        ellipsoid(d,add(wrist,mul(hand_axis,.72f*NEON_SCALE)),(vec_t){.47f,.39f,.28f},hand_basis,skin_light,6,3,false);
        vec_t thumb=add(add(wrist,mul(hand_axis,.27f*NEON_SCALE)),
                       mul(hand_side,(j?-.53f:.53f)*NEON_SCALE));
        ellipsoid(d,thumb,(vec_t){.28f,.30f,.43f},hand_basis,skin_light,6,3,false);
    }
}

static void NEON_IRAM_ATTR head(draw_t *d){
    vec_t *b=d->basis[1],centre=d->p[11];
    vec_t shoulder=mul(add(d->p[4],d->p[5]),.5f);
    tube(d,shoulder,d->p[10],.75f,.73f,skin,8);
    ellipsoid(d,centre,(vec_t){2.88f,2.60f,2.42f},b,skin_light,12,6,false);
    ellipsoid(d,local(centre,b,0,0,.30f),(vec_t){3.18f,2.90f,2.65f},b,hair_blue,12,8,true);
    /* Twin high ponytails have a continuous blue root and contrasting tips.
     * They rotate with the head in depth, including back views and jumps. */
    for(unsigned side=0;side<2;side++){
        float sign=side?1.0f:-1.0f;
        float sweep=b[2].x*.60f+b[2].y*.35f;
        vec_t root=local(centre,b,sign*2.31f,-1.50f,1.72f);
        vec_t knot=local(centre,b,sign*3.42f+sweep,-1.90f,2.06f);
        vec_t middle=local(centre,b,sign*4.76f+sweep*1.3f,-3.02f,.02f);
        vec_t transition=local(centre,b,sign*4.94f+sweep*1.6f,-3.65f,-1.53f);
        vec_t tip=local(centre,b,sign*4.63f+sweep*1.8f,-4.06f,-2.85f);
#if SCENE_PC_SMOOTH_CHARACTERS
        if(d->secondary){
            knot=secondary_hair_point(d,root,knot,b,.10f,sign,.72f);
            middle=secondary_hair_point(d,root,middle,b,.40f,sign,.72f);
            transition=secondary_hair_point(d,root,transition,b,.73f,sign,.72f);
            tip=secondary_hair_point(d,root,tip,b,1.0f,sign,.72f);
        }
#endif
        ellipsoid(d,root,(vec_t){1.05f,1.04f,1.0f},b,hair_blue,8,4,false);
        tube(d,root,knot,.78f,.91f,hair_light,8);
        tube(d,knot,middle,.96f,1.02f,hair_blue,8);
        tube(d,middle,transition,1.02f,.83f,hair_light,8);
        tube(d,transition,tip,.84f,.18f,lime,8);
        tube(d,mix(root,knot,.30f),mix(root,knot,.50f),.90f,.91f,lime,8);
    }
    /* Side-swept blue fringe sits on the forehead, preserving an exposed
     * warm face. The two small ears read through the blue silhouette. */
    vec_t fringe[]={local(centre,b,-2.55f,1.32f,1.75f),local(centre,b,.60f,2.54f,1.56f),
                    local(centre,b,-.40f,2.66f,.66f),local(centre,b,-2.45f,1.90f,.72f)};
    face(d,fringe,4,hair_light);
    ellipsoid(d,local(centre,b,0,2.66f,-.45f),(vec_t){.30f,.38f,.30f},b,skin_light,8,4,false);
    for(unsigned side=0;side<2;side++){
        float sign=side?1:-1;
        ellipsoid(d,local(centre,b,sign*1.05f,2.48f,.10f),(vec_t){.20f,.17f,.22f},b,navy,8,4,false);
    }
    for(unsigned side=0;side<2;side++){
        float sign=side?1:-1;
        ellipsoid(d,local(centre,b,sign*2.69f,.18f,-.13f),(vec_t){.34f,.49f,.68f},b,skin,8,4,false);
        box(d,local(centre,b,sign*2.94f,.16f,-.30f),(vec_t){.11f,.20f,.22f},b,lime);
    }
}
static void NEON_IRAM_ATTR skates(draw_t *d){
    for(unsigned side=0;side<2;side++){
        vec_t *b=d->basis[2+side],origin=d->p[12+side];
        box(d,local(origin,b,0,.70f,1.90f),(vec_t){.92f,2.08f,.65f},b,blue);
        ellipsoid(d,local(origin,b,0,1.95f,1.96f),(vec_t){.94f,1.15f,.65f},b,white,8,4,false);
        box(d,local(origin,b,0,-.15f,2.69f),(vec_t){.83f,.86f,.74f},b,navy_light);
        box(d,local(origin,b,0,.72f,2.57f),(vec_t){.34f,1.00f,.07f},b,lime);
        box(d,local(origin,b,0,.18f,3.43f),(vec_t){.84f,.27f,.07f},b,blue_light);
        box(d,local(origin,b,0,.22f,3.52f),(vec_t){.26f,.30f,.06f},b,lime);
        box(d,local(origin,b,0,.65f,1.11f),(vec_t){.84f,2.15f,.25f},b,metal);
        for(unsigned i=0;i<2;i++)for(unsigned j=0;j<2;j++){
            float wheel_side=i?.92f:-.92f,wheel_fwd=j?2.10f:-.75f;
            ellipsoid(d,local(origin,b,wheel_side,wheel_fwd,.57f),(vec_t){.36f,.61f,.58f},b,rubber,6,3,false);
            ellipsoid(d,local(origin,b,wheel_side+(i?.33f:-.33f),wheel_fwd,.57f),
                      (vec_t){.055f,.40f,.39f},b,lime,4,2,false);
        }
    }
}
#if SCENE_PC_SMOOTH_CHARACTERS
/* Original anime cast, desktop only. Hair, costumes, facial features and
 * accessories have separate depth-tested geometry. The four skins still use
 * exactly the same continuous rig and unchanged wheel contact positions. */
static const colour_t anime_skin={253,202,182},anime_white={246,249,255};
static const colour_t anime_ink={31,35,56},anime_gold={252,207,104};
static const colour_t anime_hair[3]={{25,173,169},{190,205,226},{230,108,172}};
static const colour_t anime_hair_light[3]={{99,232,216},{245,248,255},{255,175,215}};
static const colour_t anime_accent[3]={{36,217,198},{246,162,57},{192,131,244}};
static const colour_t anime_dark[3]={{35,47,67},{35,43,61},{94,59,137}};

static float anime_sway(const draw_t *d){
    /* Pose-derived rather than a free-running clock: scrubbing, seeking and
     * clip transitions retain the rig's continuity, with no hair phase reset. */
    const vec_t *b=d->basis[0];
    float lean=b[2].x*.20f+b[2].y*.15f;
    float feet=dot(sub(d->p[12],d->p[13]),b[1])/(NEON_SCALE*35.0f);
    float sway=lean+feet;
    return minimum(.42f,maximum(-.42f,sway));
}
static void anime_front(draw_t *d,vec_t origin,const vec_t *b,float x0,float x1,
                         float y,float z0,float z1,colour_t colour){
    quad(d,local(origin,b,x1,y,z0),local(origin,b,x0,y,z0),
           local(origin,b,x0,y,z1),local(origin,b,x1,y,z1),colour);
}
static void anime_star(draw_t *d,vec_t centre,const vec_t *b,float radius,colour_t colour){
    vec_t points[10];
    for(unsigned i=0;i<10;i++){
        float angle=NEON_PI*i*.2f,radius0=(i&1)?radius*.43f:radius;
        points[i]=local(centre,b,sinf(angle)*radius0,0,cosf(angle)*radius0);
    }
    for(unsigned i=0;i<10;i++){
        vec_t tri[]={centre,points[i],points[(i+1)%10]};face(d,tri,3,colour);
        vec_t back[]={centre,points[(i+1)%10],points[i]};face(d,back,3,colour);
    }
}
static void anime_torso(draw_t *d,vec_t hip,vec_t shoulder){
    const vec_t *b=d->basis[0];unsigned id=d->skin;
    const float section[8][2]={{-1,-.62f},{-.68f,-1},{.68f,-1},{1,-.62f},{1,.62f},{.68f,1},{-.68f,1},{-1,.62f}};
    float widths[3]={1.98f,id==1?2.72f:2.43f,id==1?2.62f:2.21f};
    const float depths[3]={1.46f,1.64f,1.31f};
    vec_t centre[3]={local(hip,b,0,0,.65f),local(shoulder,b,0,0,-.50f),local(shoulder,b,0,0,.32f)},ring[3][8];
    for(unsigned k=0;k<3;k++)for(unsigned i=0;i<8;i++)
        ring[k][i]=local(centre[k],b,section[i][0]*widths[k],section[i][1]*depths[k],0);
    for(unsigned k=0;k<2;k++)for(unsigned i=0;i<8;i++){
        unsigned j=(i+1)%8;colour_t colour=id==1?anime_dark[id]:anime_white;
        if(id==2&&(i<3||i==7))colour=anime_dark[id];
        quad(d,ring[k][i],ring[k][j],ring[k+1][j],ring[k+1][i],colour);
    }
    face(d,ring[2],8,id==1?anime_dark[id]:anime_white);
    vec_t cap[8];for(unsigned i=0;i<8;i++)cap[i]=ring[0][7-i];face(d,cap,8,anime_dark[id]);
    vec_t chest=mix(hip,shoulder,.65f);
    /* Large, readable costume shapes instead of subpixel ornamental noise. */
    if(id==0){
        vec_t tie[]={local(chest,b,0,1.75f,1.45f),local(chest,b,-.50f,1.75f,.15f),
                     local(chest,b,0,1.75f,-1.85f),local(chest,b,.50f,1.75f,.15f)};
        face(d,tie,4,anime_accent[id]);
        anime_front(d,chest,b,-1.80f,-1.30f,1.72f,-.75f,1.5f,anime_dark[id]);
        anime_front(d,chest,b,1.30f,1.80f,1.72f,-.75f,1.5f,anime_dark[id]);
    }else if(id==1){
        anime_front(d,chest,b,-.18f,.18f,1.72f,-2.7f,1.45f,anime_accent[id]);
        anime_front(d,chest,b,-1.85f,-.45f,1.75f,-.9f,.05f,(colour_t){83,102,126});
        anime_front(d,chest,b,.65f,1.85f,1.75f,.35f,.65f,anime_accent[id]);
        tube(d,local(shoulder,b,0,0,-.12f),local(shoulder,b,0,0,1.12f),1.15f,1.08f,anime_dark[id],8);
        /* Split short jacket hem and contrasting back yoke. */
        for(unsigned side=0;side<2;side++)secondary_jacket_panel(d,hip,b,side?1:-1,anime_dark[id]);
        quad(d,local(chest,b,-1.65f,-1.70f,.65f),local(chest,b,1.65f,-1.70f,.65f),
               local(chest,b,1.65f,-1.70f,1.35f),local(chest,b,-1.65f,-1.70f,1.35f),(colour_t){110,124,145});
    }else{
        anime_front(d,chest,b,-1.80f,1.80f,1.73f,.65f,1.18f,anime_dark[id]);
        anime_star(d,local(chest,b,0,1.80f,.25f),b,.67f,anime_gold);
        /* Two separate bow loops read from the back during full turns. */
        vec_t bow0=local(hip,b,-1.15f,-1.82f,.55f),bow1=local(hip,b,1.15f,-1.82f,.55f);
        if(d->secondary){
            bow0=local(bow0,b,d->motion.hem_side*.25f-d->motion.hem_flare*.12f,d->motion.hem_back*.25f,d->motion.hem_lift*.17f);
            bow1=local(bow1,b,d->motion.hem_side*.25f+d->motion.hem_flare*.12f,d->motion.hem_back*.25f,d->motion.hem_lift*.17f);
        }
        ellipsoid(d,bow0,(vec_t){1.12f,.34f,.65f},b,anime_accent[id],6,3,false);
        ellipsoid(d,bow1,(vec_t){1.12f,.34f,.65f},b,anime_accent[id],6,3,false);
    }
}
static void anime_skirt(draw_t *d,vec_t hip){
    const vec_t *b=d->basis[0];unsigned id=d->skin;float sway=anime_sway(d);
    vec_t waist[12],hem[12],trim[12];unsigned angles=circle_angles(d,12);
    for(unsigned i=0;i<12;i++){
        float c=d->circle_cos[angles][i],s=d->circle_sin[angles][i],pleat=(i&1)?.91f:1.0f;
        waist[i]=local(hip,b,c*2.05f,s*1.54f,.70f);
        hem[i]=local(hip,b,c*3.12f*pleat+sway,s*2.22f*pleat,-2.30f+sway*c*.25f);
        trim[i]=local(hip,b,c*3.17f*pleat+sway,s*2.26f*pleat,-2.62f+sway*c*.25f);
        if(d->secondary){
            const skating_secondary_motion_t *m=&d->motion;
            /* The waistband and safety shorts stay fixed. Only the hem bends;
             * limited vertical lift keeps the skirt covering the upper legs. */
            float angle=m->twist*.38f,ca=cosf(angle),sa=sinf(angle);
            float dx=m->hem_side+c*m->hem_flare*.55f+(c*ca-s*sa-c)*2.8f;
            float dy=m->hem_back+s*m->hem_flare*.44f+(c*sa+s*ca-s)*2.0f;
            float dz=m->hem_lift*.36f+m->hem_flare*.13f;
            hem[i]=local(hem[i],b,dx*.92f,dy*.92f,dz*.92f);
            trim[i]=local(trim[i],b,dx,dy,dz);
        }
    }
    for(unsigned i=0;i<12;i++){
        unsigned j=(i+1)%12;colour_t cloth=id==0?anime_white:anime_dark[id];
        if(i&1)cloth=id==0?(colour_t){185,208,217}:(colour_t){128,85,166};
        quad(d,waist[i],hem[i],hem[j],waist[j],cloth);
        quad(d,hem[i],trim[i],trim[j],hem[j],id==0?anime_accent[id]:anime_gold);
    }
    /* Dark safety shorts stay under the skirt in spins and high free legs. */
    ellipsoid(d,hip,(vec_t){2.05f,1.55f,1.35f},b,anime_dark[id],8,4,false);
}
static void anime_hand(draw_t *d,unsigned side,vec_t elbow,vec_t wrist){
    const vec_t *b=d->basis[0];vec_t axis=unit(sub(wrist,elbow));
    vec_t across=sub(b[0],mul(axis,dot(b[0],axis)));
    if(dot(across,across)<1e-8f)across=cross(b[1],axis);
    across=unit(across);vec_t hb[]={across,unit(cross(axis,across)),axis};
    ellipsoid(d,add(wrist,mul(axis,.25f*NEON_SCALE)),(vec_t){.57f,.44f,.71f},hb,anime_skin,6,3,false);
    ellipsoid(d,add(add(wrist,mul(axis,.19f*NEON_SCALE)),mul(across,(side?-.49f:.49f)*NEON_SCALE)),
              (vec_t){.25f,.26f,.37f},hb,anime_skin,6,3,false);
}
static void classic_clothes(draw_t *d){
    vec_t hip=mul(add(d->p[0],d->p[1]),.5f),shoulder=mul(add(d->p[4],d->p[5]),.5f);
    const vec_t *b=d->basis[0];unsigned id=d->skin;
    anime_torso(d,hip,shoulder);
    if(id!=1)anime_skirt(d,hip);
    else ellipsoid(d,hip,(vec_t){2.18f,1.65f,1.4f},b,anime_dark[id],8,4,false);
    for(unsigned j=0;j<2;j++){
        vec_t knee=d->p[2+j],ankle=local(d->p[12+j],d->basis[2+j],0,-.15f,2.69f);
        if(id==1){
            colour_t pants=j?(colour_t){57,68,87}:(colour_t){84,97,117};
            tube(d,d->p[j],knee,1.23f,1.10f,pants,8);
            ellipsoid(d,knee,(vec_t){1.13f,1.13f,1.07f},b,anime_dark[id],6,3,false);
            tube(d,knee,ankle,1.07f,.74f,pants,8);
            ellipsoid(d,local(knee,b,0,.82f,.02f),(vec_t){.79f,.40f,1.0f},b,(colour_t){105,124,145},6,3,false);
            vec_t outside=mul(b[0],(j?1:-1)*1.06f*NEON_SCALE);
            tube(d,add(mix(d->p[j],knee,.25f),outside),add(mix(d->p[j],knee,.75f),outside),.13f,.13f,anime_accent[id],4);
        }else{
            vec_t stocking=mix(d->p[j],knee,id==0?.53f:.84f);
            tube(d,d->p[j],stocking,1.13f,1.02f,anime_skin,8);
            tube(d,stocking,knee,1.04f,.99f,id==0?anime_dark[id]:anime_white,8);
            ellipsoid(d,knee,(vec_t){1.0f,1.0f,.94f},b,id==0?anime_dark[id]:anime_white,6,3,false);
            tube(d,knee,ankle,.98f,.69f,id==0?anime_dark[id]:anime_white,8);
            tube(d,stocking,mix(stocking,knee,.12f),1.08f,1.07f,anime_accent[id],6);
        }
        vec_t upper=d->p[4+j],elbow=d->p[6+j],wrist=d->p[8+j];
        if(id==1){
            vec_t sleeve=mix(elbow,wrist,.36f);
            tube(d,upper,elbow,1.05f,.88f,anime_dark[id],8);
            ellipsoid(d,elbow,(vec_t){.90f,.90f,.87f},b,anime_dark[id],6,3,false);
            tube(d,elbow,sleeve,.88f,.78f,anime_dark[id],8);
            tube(d,sleeve,wrist,.66f,.54f,anime_skin,6);
            tube(d,mix(elbow,wrist,.25f),sleeve,.82f,.81f,anime_accent[id],6);
        }else{
            vec_t sleeve=mix(upper,elbow,id==0?.21f:.37f);
            tube(d,upper,sleeve,id==0?.89f:1.09f,id==0?.84f:1.01f,anime_white,8);
            tube(d,sleeve,elbow,.70f,.61f,anime_skin,6);
            ellipsoid(d,elbow,(vec_t){.64f,.64f,.64f},b,anime_skin,6,3,false);
            if(id==0){
                tube(d,elbow,wrist,.70f,.56f,anime_dark[id],8);
                tube(d,elbow,mix(elbow,wrist,.12f),.74f,.72f,anime_accent[id],6);
            }else tube(d,elbow,wrist,.61f,.52f,anime_skin,6);
            tube(d,mix(elbow,wrist,.84f),mix(elbow,wrist,.96f),.63f,.62f,id==0?anime_accent[id]:anime_gold,6);
        }
        anime_hand(d,j,elbow,wrist);
    }
    tube(d,shoulder,d->p[10],.73f,.71f,anime_skin,8);
}
static void anime_face(draw_t *d,vec_t centre,const vec_t *b){
    unsigned id=d->skin;
    ellipsoid(d,centre,(vec_t){2.88f,2.59f,2.48f},b,anime_skin,12,6,false);
    for(unsigned j=0;j<2;j++){
        float sign=j?1:-1,x=sign*1.08f;
        ellipsoid(d,local(centre,b,x,2.42f,.04f),(vec_t){.52f,.18f,.62f},b,anime_white,8,4,false);
        ellipsoid(d,local(centre,b,x+sign*.02f,2.61f,.02f),(vec_t){.30f,.10f,.44f},b,anime_accent[id],8,4,false);
        ellipsoid(d,local(centre,b,x,2.70f,.11f),(vec_t){.14f,.065f,.31f},b,anime_ink,6,3,false);
        ellipsoid(d,local(centre,b,x-.11f,2.77f,.27f),(vec_t){.10f,.045f,.12f},b,anime_white,6,3,false);
        tube(d,local(centre,b,x-sign*.47f,2.57f,.55f),local(centre,b,x+sign*.50f,2.56f,.53f),.105f,.11f,anime_ink,6);
        if(id!=1)tube(d,local(centre,b,x+sign*.43f,2.55f,.51f),local(centre,b,x+sign*.67f,2.49f,.70f),.09f,.025f,anime_ink,4);
        ellipsoid(d,local(centre,b,sign*1.70f,2.16f,-.69f),(vec_t){.39f,.07f,.15f},b,(colour_t){242,151,156},6,3,false);
        ellipsoid(d,local(centre,b,sign*2.68f,0,-.05f),(vec_t){.34f,.48f,.65f},b,anime_skin,6,3,false);
    }
    ellipsoid(d,local(centre,b,0,2.61f,-.60f),(vec_t){.20f,.22f,.26f},b,anime_skin,6,3,false);
    tube(d,local(centre,b,-.31f,2.46f,-1.15f),local(centre,b,.31f,2.46f,-1.15f),.075f,.075f,(colour_t){162,78,105},6);
}
static void anime_fringe(draw_t *d,vec_t centre,const vec_t *b){
    unsigned id=d->skin;
    for(unsigned j=0;j<5;j++){
        float x=-2.10f+j*1.02f,tip_x=x+(id==1?.55f:-.35f),z=(j==2?.87f:1.05f);
        vec_t root=local(centre,b,x,1.80f,1.90f),tip=local(centre,b,tip_x,2.56f,z);
        if(id==1&&d->secondary)tip=secondary_hair_point(d,root,tip,b,.30f,x<0?-1:1,.22f);
        tube(d,root,tip,.59f,.035f,(j&1)?anime_hair_light[id]:anime_hair[id],6);
    }
}
static void classic_head(draw_t *d){
    vec_t centre=d->p[11],*b=d->basis[1];unsigned id=d->skin;float sway=anime_sway(d);
    anime_face(d,centre,b);
    ellipsoid(d,local(centre,b,0,-.12f,.30f),(vec_t){3.13f,2.93f,2.70f},b,anime_hair[id],12,8,true);
    anime_fringe(d,centre,b);
    if(id==0){
        /* Long green twin tails differ in silhouette from Neon's short,
         * outward blue ponytails. Narrow lower tips leave both arms visible. */
        for(unsigned j=0;j<2;j++){
            float sign=j?1:-1;
            vec_t root=local(centre,b,sign*2.60f,-1.35f,1.48f);
            vec_t upper=local(centre,b,sign*3.66f,-1.85f,-.95f);
            vec_t lower=local(centre,b,sign*3.88f+sway,-2.17f,-4.22f);
            vec_t tip=local(centre,b,sign*4.12f+sway*1.6f,-2.50f,-6.68f);
            if(d->secondary){
                upper=secondary_hair_point(d,root,upper,b,.22f,sign,1.0f);
                lower=secondary_hair_point(d,root,lower,b,.60f,sign,1.0f);
                tip=secondary_hair_point(d,root,tip,b,1.0f,sign,1.0f);
            }
            tube(d,root,upper,.72f,1.00f,anime_hair[id],8);
            tube(d,upper,lower,1.00f,.80f,anime_hair_light[id],8);
            tube(d,lower,tip,.80f,.12f,anime_hair[id],8);
            tube(d,root,mix(root,upper,.17f),.88f,.90f,anime_dark[id],6);
            /* Actual ear cups and a visible silver headband. */
            box(d,local(centre,b,sign*2.98f,.14f,.08f),(vec_t){.37f,.69f,.86f},b,anime_dark[id]);
            box(d,local(centre,b,sign*3.37f,.14f,.08f),(vec_t){.07f,.46f,.55f},b,anime_accent[id]);
            tube(d,local(centre,b,sign*2.81f,-.28f,1.00f),local(centre,b,sign*1.90f,-.28f,2.72f),.18f,.18f,anime_dark[id],6);
            tube(d,local(centre,b,sign*1.90f,-.28f,2.72f),local(centre,b,0,-.28f,3.15f),.18f,.18f,anime_dark[id],6);
        }
    }else if(id==1){
        /* Broad, asymmetric silver tufts produce a spiky boy silhouette from
         * front, rear and profile; no ponytail geometry is shared here. */
        const float roots[7][3]={{-2.10f,0,1.55f},{-1.30f,-.60f,2.20f},{0,-.70f,2.40f},{1.10f,-.50f,2.20f},{2.15f,-.15f,1.50f},{-1.90f,-1.55f,1.10f},{1.45f,-1.90f,1.05f}};
        const float tips[7][3]={{-3.68f,.15f,2.45f},{-2.03f,-.87f,3.85f},{.53f,-.92f,4.12f},{2.24f,-.55f,3.65f},{3.65f,.16f,2.19f},{-3.00f,-2.42f,1.40f},{2.63f,-3.00f,1.57f}};
        for(unsigned i=0;i<7;i++){
            vec_t root=local(centre,b,roots[i][0],roots[i][1],roots[i][2]);
            vec_t tip=local(centre,b,tips[i][0],tips[i][1],tips[i][2]);
            if(d->secondary)tip=secondary_hair_point(d,root,tip,b,.70f,tips[i][0]<0?-1:1,.27f);
            tube(d,root,tip,.80f,.025f,i&1?anime_hair_light[id]:anime_hair[id],6);
        }
        box(d,local(centre,b,-2.94f,.05f,-.08f),(vec_t){.20f,.34f,.50f},b,anime_dark[id]);
        box(d,local(centre,b,-3.15f,.05f,-.08f),(vec_t){.035f,.23f,.28f},b,anime_accent[id]);
    }else{
        /* Pink shoulder-length back locks, plus one unmistakable side tie. */
        for(unsigned j=0;j<3;j++){
            float x=(j-1.0f)*1.85f;
            vec_t root=local(centre,b,x,-1.60f,.20f),middle=local(centre,b,x*1.13f+sway*.4f,-1.98f,-2.50f);
            vec_t tip=local(centre,b,x*1.18f+sway*.8f,-2.00f,-4.28f);
            if(d->secondary){
                middle=secondary_hair_point(d,root,middle,b,.45f,x<0?-1:1,.66f);
                tip=secondary_hair_point(d,root,tip,b,1.0f,x<0?-1:1,.66f);
            }
            tube(d,root,middle,.96f,.89f,anime_hair[id],8);
            tube(d,middle,tip,.89f,.12f,j&1?anime_hair_light[id]:anime_hair[id],8);
        }
        vec_t root=local(centre,b,2.50f,-.65f,1.08f),middle=local(centre,b,4.18f+sway,.05f,-.65f);
        vec_t tip=local(centre,b,4.35f+sway*1.6f,.11f,-3.15f);
        if(d->secondary){
            middle=secondary_hair_point(d,root,middle,b,.40f,1,.90f);
            tip=secondary_hair_point(d,root,tip,b,1.0f,1,.90f);
        }
        tube(d,root,middle,.75f,.92f,anime_hair_light[id],8);
        tube(d,middle,tip,.92f,.10f,anime_hair[id],8);
        ellipsoid(d,root,(vec_t){.91f,.78f,.68f},b,anime_dark[id],6,3,false);
        anime_star(d,local(centre,b,-2.18f,1.90f,1.65f),b,.91f,anime_gold);
    }
}
static void classic_skates(draw_t *d){
    unsigned id=d->skin;colour_t boot=id==1?anime_dark[id]:anime_white;
    for(unsigned j=0;j<2;j++){
        vec_t origin=d->p[12+j],*b=d->basis[2+j];
        box(d,local(origin,b,0,.70f,1.90f),(vec_t){.92f,2.08f,.65f},b,boot);
        ellipsoid(d,local(origin,b,0,1.95f,1.96f),(vec_t){.94f,1.15f,.65f},b,boot,8,4,false);
        box(d,local(origin,b,0,-.15f,2.69f),(vec_t){.83f,.86f,.74f},b,boot);
        box(d,local(origin,b,0,.72f,2.57f),(vec_t){.32f,1.00f,.075f},b,id==2?anime_gold:anime_accent[id]);
        box(d,local(origin,b,0,.16f,3.43f),(vec_t){.84f,.25f,.075f},b,anime_accent[id]);
        box(d,local(origin,b,0,.65f,1.11f),(vec_t){.84f,2.15f,.25f},b,(colour_t){149,163,181});
        for(unsigned side=0;side<2;side++)for(unsigned fwd=0;fwd<2;fwd++){
            float x=side?.92f:-.92f,y=fwd?2.10f:-.75f;
            ellipsoid(d,local(origin,b,x,y,.57f),(vec_t){.36f,.61f,.58f},b,anime_ink,8,4,false);
            ellipsoid(d,local(origin,b,x+(side?.33f:-.33f),y,.57f),(vec_t){.05f,.40f,.39f},b,
                      id==2?anime_gold:anime_accent[id],4,2,false);
        }
    }
}
#endif
static void geometry(draw_t *d){
    draw_shadows(d);
#if SCENE_PC_SMOOTH_CHARACTERS
    if(d->skin<3){classic_clothes(d);classic_head(d);classic_skates(d);return;}
#endif
    clothes(d);head(d);skates(d);
}

static vec_t rig_point(const neon_rig_frame_t *rig,unsigned i){
    return (vec_t){rig->point[i][0]*NEON_POINT_SCALE,rig->point[i][1]*NEON_POINT_SCALE,rig->point[i][2]*NEON_POINT_SCALE};
}
static vec_t rig_axis(const neon_rig_frame_t *rig,unsigned basis,unsigned axis){
    return (vec_t){rig->basis[basis][axis][0]*NEON_AXIS_SCALE,
                   rig->basis[basis][axis][1]*NEON_AXIS_SCALE,
                   rig->basis[basis][axis][2]*NEON_AXIS_SCALE};
}
static void interpolate_rig(draw_t *d,unsigned frame0,unsigned frame1,float blend){
    const neon_rig_frame_t *a=&neon_rig_frames[frame0],*b=&neon_rig_frames[frame1];
    for(unsigned i=0;i<14;i++)d->p[i]=mix(rig_point(a,i),rig_point(b,i),blend);
    for(unsigned i=0;i<4;i++){
        /* The small inter-frame rotations take the short arc. Rebuild the
         * complete right-handed frame after interpolation: normalizing each
         * quantized axis independently would still allow skewed shoe cuffs. */
        vec_t right=unit(mix(rig_axis(a,i,0),rig_axis(b,i,0),blend));
        vec_t forward=mix(rig_axis(a,i,1),rig_axis(b,i,1),blend);
        forward=unit(sub(forward,mul(right,dot(right,forward))));
        vec_t up=unit(cross(right,forward));
        d->basis[i][0]=right;d->basis[i][1]=forward;d->basis[i][2]=up;
    }
}

#if SCENE_PC_SMOOTH_CHARACTERS
typedef struct {float x,v;} secondary_spring_t;
static float secondary_angle_delta(float current,float previous){
    float delta=current-previous;
    while(delta>NEON_PI)delta-=2*NEON_PI;
    while(delta<-NEON_PI)delta+=2*NEON_PI;
    return delta;
}
static void secondary_spring_step(secondary_spring_t *spring,float target,float frequency,
                                   float damping,float dt,float limit){
    spring->v+=(frequency*frequency*(target-spring->x)-2*damping*frequency*spring->v)*dt;
    spring->v=secondary_limit(spring->v,limit*18);
    spring->x+=spring->v*dt;
    if(spring->x>limit){spring->x=limit;if(spring->v>0)spring->v=0;}
    if(spring->x<-limit){spring->x=-limit;if(spring->v<0)spring->v=0;}
}
static skating_secondary_motion_t secondary_sanitized(const skating_secondary_motion_t *m){
    /* Public draw inputs are bounded even when a caller supplies its own
     * motion. The same copied values drive measurement and rasterization. */
#define SECONDARY_VALUE(field,cap) (isfinite(m->field)?secondary_limit(m->field,cap):0.0f)
    skating_secondary_motion_t result={
        SECONDARY_VALUE(hair_side,1.35f),SECONDARY_VALUE(hair_back,1.25f),
        SECONDARY_VALUE(hair_lift,1.05f),SECONDARY_VALUE(hair_flare,1.20f),
        SECONDARY_VALUE(hem_side,.65f),SECONDARY_VALUE(hem_back,.60f),
        SECONDARY_VALUE(hem_lift,.65f),SECONDARY_VALUE(hem_flare,.80f),
        SECONDARY_VALUE(twist,.28f),SECONDARY_VALUE(jacket_sway,.75f)};
#undef SECONDARY_VALUE
    return result;
}
#endif

bool skating_character_secondary_motion(const skating_motion_sample_t *history,
                                         unsigned count,float step_seconds,
                                         skating_secondary_motion_t *out){
    if(!out)return false;
    memset(out,0,sizeof(*out));
    if(!history||!count||count>512||!isfinite(step_seconds)||step_seconds<.001f||step_seconds>.1f)return false;
#if SCENE_PC_SMOOTH_CHARACTERS
    const unsigned frames=sizeof(neon_rig_frames)/sizeof(neon_rig_frames[0]);
    for(unsigned i=0;i<count;i++)if(history[i].frame0>=frames||history[i].frame1>=frames||
            !isfinite(history[i].blend)||!isfinite(history[i].u)||!isfinite(history[i].v)||
            fabsf(history[i].u)>100000||fabsf(history[i].v)>100000)return false;
    if(count<2)return true;
    /* Each call reconstructs a short, fixed PCM history. A cold window starts
     * with its first measured velocity, so constant travel cannot fabricate
     * an acceleration or an initial landing impulse. No mutable state lives
     * outside this call. Pose quantization is filtered before differentiation. */
    secondary_spring_t spring[10]={{0}};
    static const float frequencies[10]={10.8f,10.2f,12.0f,8.5f,13.5f,12.8f,14.0f,10.0f,10.5f,15.0f};
    static const float dampings[10]={.60f,.62f,.57f,.78f,.73f,.74f,.64f,.78f,.66f,.74f};
    static const float limits[10]={1.35f,1.25f,1.05f,1.20f,.65f,.60f,.65f,.80f,.28f,.75f};
    draw_t pose={0};
    vec_t prev_head={0},prev_hip={0},head_velocity={0},hip_velocity={0};
    vec_t head_acceleration={0},hip_acceleration={0};
    float prev_head_angle=0,prev_body_angle=0,head_omega=0,body_omega=0;
    float head_alpha=0,body_alpha=0,prev_height=0,height_velocity=0;
    bool have_air=false;
    float velocity_mix=1-expf(-step_seconds/.065f),acceleration_mix=1-expf(-step_seconds/.075f);
    float angular_mix=1-expf(-step_seconds/.080f),angular_accel_mix=1-expf(-step_seconds/.095f);
    for(unsigned i=0;i<count;i++){
        const skating_motion_sample_t *sample=&history[i];
        float blend=minimum(1,maximum(0,sample->blend));
        interpolate_rig(&pose,sample->frame0,sample->frame1,blend);
        vec_t route={sample->u,sample->v,0};
        vec_t head_position=mul(add(pose.p[11],route),1.0f/NEON_SCALE);
        vec_t hip_position=mul(add(mul(add(pose.p[0],pose.p[1]),.5f),route),1.0f/NEON_SCALE);
        float head_angle=atan2f(pose.basis[1][0].y,pose.basis[1][0].x);
        float body_angle=atan2f(pose.basis[0][0].y,pose.basis[0][0].x);
        float height=minimum(skate_floor_height(&pose,0),skate_floor_height(&pose,1))/NEON_SCALE;
        if(i==0){
            prev_head=head_position;prev_hip=hip_position;
            prev_head_angle=head_angle;prev_body_angle=body_angle;prev_height=height;
            have_air=height>.65f;continue;
        }
        vec_t raw_head=mul(sub(head_position,prev_head),1.0f/step_seconds);
        vec_t raw_hip=mul(sub(hip_position,prev_hip),1.0f/step_seconds);
        float raw_head_omega=secondary_angle_delta(head_angle,prev_head_angle)/step_seconds;
        float raw_body_omega=secondary_angle_delta(body_angle,prev_body_angle)/step_seconds;
        float raw_height_velocity=(height-prev_height)/step_seconds;
        if(i==1){
            head_velocity=raw_head;hip_velocity=raw_hip;
            head_omega=raw_head_omega;body_omega=raw_body_omega;height_velocity=raw_height_velocity;
        }
        vec_t old_head_velocity=head_velocity,old_hip_velocity=hip_velocity;
        float old_head_omega=head_omega,old_body_omega=body_omega,old_height_velocity=height_velocity;
        head_velocity=mix(head_velocity,raw_head,velocity_mix);hip_velocity=mix(hip_velocity,raw_hip,velocity_mix);
        head_acceleration=mix(head_acceleration,mul(sub(head_velocity,old_head_velocity),1.0f/step_seconds),acceleration_mix);
        hip_acceleration=mix(hip_acceleration,mul(sub(hip_velocity,old_hip_velocity),1.0f/step_seconds),acceleration_mix);
        head_omega+=(raw_head_omega-head_omega)*angular_mix;body_omega+=(raw_body_omega-body_omega)*angular_mix;
        head_alpha+=((head_omega-old_head_omega)/step_seconds-head_alpha)*angular_accel_mix;
        body_alpha+=((body_omega-old_body_omega)/step_seconds-body_alpha)*angular_accel_mix;
        height_velocity+=(raw_height_velocity-height_velocity)*velocity_mix;
        vec_t *hb=pose.basis[1],*bb=pose.basis[0];
        float hx=dot(head_acceleration,hb[0]),hy=dot(head_acceleration,hb[1]);
        float bx=dot(hip_acceleration,bb[0]),by=dot(hip_acceleration,bb[1]);
        float spin=minimum(12,fabsf(head_omega)),body_spin=minimum(12,fabsf(body_omega));
        float air=minimum(1,height/1.5f),apex=air*maximum(0,1-fabsf(height_velocity)/4.5f);
        float target[10]={
            -hx*.016f-dot(head_velocity,hb[0])*.012f-head_alpha*.028f-head_omega*.055f,
            -hy*.016f-dot(head_velocity,hb[1])*.017f-spin*.018f,
            -head_acceleration.z*.017f-head_velocity.z*.024f+apex*.44f-air*maximum(0,height_velocity)*.010f,
            spin*spin*.014f,
            -bx*.010f-dot(hip_velocity,bb[0])*.006f-body_alpha*.015f-body_omega*.030f,
            -by*.010f-dot(hip_velocity,bb[1])*.009f,
            -hip_acceleration.z*.009f+apex*.20f-hip_velocity.z*.009f,
            body_spin*body_spin*.010f,
            -head_omega*.026f-head_alpha*.009f,
            -bx*.012f-body_alpha*.014f-body_omega*.025f};
        /* A real descending ground crossing adds a small compression kick.
         * The underdamped spring then releases it without a scripted bounce. */
        if(have_air&&prev_height>.25f&&height<=.25f&&old_height_velocity<-1.0f){
            float impact=minimum(10,-old_height_velocity);
            spring[2].v-=impact*.13f;spring[6].v-=impact*.10f;
            spring[0].v+=secondary_limit(head_omega,.5f)*.10f;have_air=false;
        }
        if(height>.65f)have_air=true;
        /* Stable substeps also support callers sampling a coarser history. */
        unsigned substeps=(unsigned)ceilf(step_seconds/(1.0f/120.0f));
        float dt=step_seconds/substeps;
        for(unsigned j=0;j<10;j++)for(unsigned sub=0;sub<substeps;sub++)
            secondary_spring_step(&spring[j],secondary_limit(target[j],limits[j]),frequencies[j],dampings[j],dt,limits[j]);
        prev_head=head_position;prev_hip=hip_position;prev_head_angle=head_angle;prev_body_angle=body_angle;prev_height=height;
    }
    *out=(skating_secondary_motion_t){spring[0].x,spring[1].x,spring[2].x,maximum(0,spring[3].x),
        spring[4].x,spring[5].x,spring[6].x,maximum(0,spring[7].x),spring[8].x,spring[9].x};
#endif
    return true;
}

bool NEON_IRAM_ATTR skating_character_draw_with_secondary(unsigned frame0,unsigned frame1,float blend,float scale,
                                      unsigned skin_id,float anchor_x,float anchor_y,
                                      const skating_secondary_motion_t *motion,
                                      uint8_t *scratch,size_t bytes,int *x,int *y,unsigned *w,unsigned *h){
    const unsigned frames=sizeof(neon_rig_frames)/sizeof(neon_rig_frames[0]);
    if(frame0>=frames||frame1>=frames||!isfinite(blend)||!isfinite(scale)||scale<.1f||scale>2.0f||
       skin_id>3||!isfinite(anchor_x)||!isfinite(anchor_y)||fabsf(anchor_x)>1||fabsf(anchor_y)>1||
       !scratch||!x||!y||!w||!h)return false;
#if !SCENE_PC_SMOOTH_CHARACTERS
    if(skin_id!=3)return false;
#endif
    if(blend<0)blend=0;
    if(blend>1)blend=1;
    draw_t d={0};d.scale=scale;d.skin=skin_id;d.anchor_x=anchor_x;d.anchor_y=anchor_y;
#if SCENE_PC_SMOOTH_CHARACTERS
    if(motion){d.secondary=true;d.motion=secondary_sanitized(motion);}
#else
    (void)motion;
#endif
    d.measuring=true;d.left=d.top=INT_MAX;d.right=d.bottom=INT_MIN;
    /* The measuring pass prepares exact projected faces in the free tail.
     * No cache survives the call. Tiny buffers fall back without tail writes. */
    uintptr_t begin=(uintptr_t)scratch,end=begin;
    if(bytes<=UINTPTR_MAX-begin){end=(begin+bytes)&~(uintptr_t)(_Alignof(vec_t)-1u);if(end<begin)end=begin;}
    d.prepared_end=(uint8_t*)end;
    d.prepared_capacity=end-begin;
    interpolate_rig(&d,frame0,frame1,blend);
    geometry(&d);d.x=d.left-1;d.y=d.top-1;d.w=(unsigned)(d.right-d.x+1);d.h=(unsigned)(d.bottom-d.y+1);
    if(d.w<1||d.h<1||d.w>255||d.h>255)return false;
    size_t pixels=(size_t)d.w*d.h,spixels=pixels*4,output=pixels*3;
    if(pixels>SIZE_MAX/23||output+spixels*5+3>bytes)return false;
    /* Keep temporary arrays after the output range so downsampling never
     * overwrites unconsumed high-resolution samples. Align uint16 buffers. */
    size_t offset=(output+1u)&~(size_t)1u;
    d.colour=(uint16_t*)(void*)(scratch+offset);d.depth=(int16_t*)(void*)(scratch+offset+spixels*2);
    d.alpha=scratch+offset+spixels*4;d.sw=d.w*2;d.sh=d.h*2;
    for(size_t i=0;i<spixels;i++)d.depth[i]=INT16_MIN;
    memset(d.alpha,0,spixels);d.measuring=false;
    bool prepared=!d.prepared_overflow&&begin+offset+spixels*5<=(uintptr_t)(d.prepared_end-d.prepared_used);
    if(prepared){
        /* Preserve original shadow/triangle draw order and depth tie handling. */
        draw_shadows(&d);
        const uint8_t *cursor=d.prepared_end;
        for(size_t i=0;i<d.prepared_count;i++){
            uint32_t header=*(const uint32_t*)(const void*)(cursor-sizeof(uint32_t));
            unsigned count=header>>16;uint16_t colour=(uint16_t)header;
            const vec_t *v=(const vec_t*)(const void*)(cursor-sizeof(uint32_t)-count*sizeof(vec_t));
            vec_t a=v[0],b=v[1];
            for(unsigned j=2;j<count;j++){vec_t c=v[j];triangle(&d,a,b,c,colour);b=c;}
            cursor=(const uint8_t*)v;
        }
    }else geometry(&d);
    for(unsigned row=0;row<d.h;row++)for(unsigned col=0;col<d.w;col++){
        size_t a=(size_t)row*2*d.sw+col*2,indices[]={a,a+1,a+d.sw,a+d.sw+1};
        unsigned mask=d.alpha[a]|d.alpha[a+1]|d.alpha[a+d.sw]|d.alpha[a+d.sw+1];
        size_t out=((size_t)row*d.w+col)*3;
        if(!mask){scratch[out]=scratch[out+1]=scratch[out+2]=0;continue;}
        uint16_t solid=d.colour[a];
        if(d.alpha[a]==255&&d.alpha[a+1]==255&&d.alpha[a+d.sw]==255&&d.alpha[a+d.sw+1]==255
           &&solid==d.colour[a+1]&&solid==d.colour[a+d.sw]&&solid==d.colour[a+d.sw+1]){
            scratch[out]=(uint8_t)solid;scratch[out+1]=(uint8_t)(solid>>8);scratch[out+2]=255;continue;
        }
        unsigned sum=0,r=0,g=0,b=0;
        for(unsigned k=0;k<4;k++){
            unsigned alpha=d.alpha[indices[k]];uint16_t c=alpha?d.colour[indices[k]]:0;
            sum+=alpha;r+=((c>>11)&31)*alpha;g+=((c>>5)&63)*alpha;b+=(c&31)*alpha;
        }
        uint16_t colour=sum?(uint16_t)((((r+sum/2)/sum)<<11)|(((g+sum/2)/sum)<<5)|((b+sum/2)/sum)):0;
        scratch[out]=(uint8_t)colour;scratch[out+1]=(uint8_t)(colour>>8);scratch[out+2]=(uint8_t)((sum+2)/4);
    }
    *x=d.x;*y=d.y;*w=d.w;*h=d.h;return true;
}

bool NEON_IRAM_ATTR skating_character_draw_interpolated(unsigned frame0,unsigned frame1,float blend,float scale,
                                      unsigned skin_id,float anchor_x,float anchor_y,
                                      uint8_t *scratch,size_t bytes,int *x,int *y,unsigned *w,unsigned *h){
    return skating_character_draw_with_secondary(frame0,frame1,blend,scale,skin_id,anchor_x,anchor_y,NULL,
                                                 scratch,bytes,x,y,w,h);
}

bool neon_character_draw(unsigned frame,uint8_t *scratch,size_t bytes,int *x,int *y,unsigned *w,unsigned *h){
    return neon_character_draw_interpolated(frame,frame,0,1,scratch,bytes,x,y,w,h);
}

bool neon_character_draw_interpolated(unsigned frame0,unsigned frame1,float blend,float scale,
                                     uint8_t *scratch,size_t bytes,int *x,int *y,unsigned *w,unsigned *h){
    return skating_character_draw_interpolated(frame0,frame1,blend,scale,3,0,0,scratch,bytes,x,y,w,h);
}
