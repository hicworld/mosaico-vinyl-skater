/* Original moving scenery. Broad moving silhouettes carry the animation;
 * small snow/rain/fireflies only add depth. All drawing stays deterministic
 * through pauses and arbitrary seeks because there is no simulation state. */
#include "scene_ambience.h"
#include <math.h>
#include <stddef.h>

#define PI 3.14159265358979323846f
#define RGB(r,g,b) ((uint16_t)((((r)>>3)<<11)|(((g)>>2)<<5)|((b)>>3)))
typedef struct {float x,y;} point_t;
typedef struct {scene_ambience_plot_t plot;void *context;const uint16_t (*inside)[2];} canvas_t;
static float clamp(float x,float low,float high){return x<low?low:(x>high?high:x);}
static float minf(float a,float b){return a<b?a:b;}
static float maxf(float a,float b){return a>b?a:b;}
static point_t point(float x,float y){return (point_t){x,y};}
static point_t add(point_t a,point_t b){return point(a.x+b.x,a.y+b.y);}
static point_t mul(point_t a,float scale){return point(a.x*scale,a.y*scale);}
static void pixel(canvas_t *c,int x,int y,uint16_t colour,unsigned alpha){
    if(x<0||x>=480||y<0||y>=480||!alpha)return;
    if(c->inside&&(unsigned)x>=c->inside[y][0]&&(unsigned)x<c->inside[y][1])return;
    c->plot(c->context,x,y,colour,alpha>255?255:alpha);
}
static void solid_span(canvas_t *c,int y,int first,int last,uint16_t colour,unsigned alpha){
    if(c->inside){
        int left=c->inside[y][0],right=c->inside[y][1];
        if(left<right&&first<right&&last>left){
            int end=last<left?last:left;
            for(int x=first;x<end;x++)c->plot(c->context,x,y,colour,alpha);
            if(first<right)first=right;
        }
    }
    for(int x=first;x<last;x++)c->plot(c->context,x,y,colour,alpha);
}
static void span(canvas_t *c,int y,float left,float right,uint16_t colour,unsigned alpha){
    if(y<0||y>=480||right<=left||!alpha)return;
    int first=(int)floorf(maxf(0,left)),last=(int)ceilf(minf(480,right));
    /* Only the two edge pixels can have fractional coverage. Interior
     * pixels have already been clipped and retain the exact integer alpha. */
    int x=first;
    if(x<last&&x<left){
        float coverage=minf(x+1.0f,right)-maxf((float)x,left);
        pixel(c,x++,y,colour,(unsigned)lroundf(alpha*clamp(coverage,0,1)));
    }
    int whole=(int)floorf(minf(480,right));
    unsigned solid=alpha>255?255:alpha;
    solid_span(c,y,x,whole,colour,solid);
    if(x<whole)x=whole;
    if(x<last){
        float coverage=minf(x+1.0f,right)-maxf((float)x,left);
        pixel(c,x,y,colour,(unsigned)lroundf(alpha*clamp(coverage,0,1)));
    }
}
static void polygon(canvas_t *c,const point_t *p,unsigned n,uint16_t colour,unsigned alpha){
    float top=480,bottom=0;
    for(unsigned i=0;i<n;i++){top=minf(top,p[i].y);bottom=maxf(bottom,p[i].y);}
    int first=(int)floorf(maxf(0,top)),last=(int)ceilf(minf(480,bottom));
    for(int y=first;y<last;y++){
        float scan=y+.5f,xs[16];unsigned count=0;
        for(unsigned i=0,j=n-1;i<n;j=i++)if((p[i].y<=scan&&p[j].y>scan)||(p[j].y<=scan&&p[i].y>scan)){
            if(count<16)xs[count++]=p[i].x+(scan-p[i].y)*(p[j].x-p[i].x)/(p[j].y-p[i].y);
        }
        for(unsigned i=1;i<count;i++){float value=xs[i];unsigned j=i;while(j&&xs[j-1]>value){xs[j]=xs[j-1];j--;}xs[j]=value;}
        for(unsigned i=0;i+1<count;i+=2)span(c,y,xs[i],xs[i+1],colour,alpha);
    }
}
static void oval(canvas_t *c,float x,float y,float rx,float ry,uint16_t colour,unsigned alpha){
    int first=(int)floorf(maxf(0,y-ry)),last=(int)ceilf(minf(480,y+ry));
    for(int row=first;row<last;row++){
        float dy=(row+.5f-y)/ry;
        if(fabsf(dy)>=1)continue;
        float width=rx*sqrtf(1-dy*dy);
        float edge=clamp(minf(row+1.0f,y+ry)-maxf((float)row,y-ry),0,1);
        span(c,row,x-width,x+width,colour,(unsigned)lroundf(alpha*edge));
    }
}
static void line_row(canvas_t *c,int y,int left,int right,point_t a,
                       float dx,float dy,float length,float r,uint16_t colour,unsigned alpha){
    for(int x=left;x<right;x++){
        float px=x+.5f-a.x,py=y+.5f-a.y,t=length>1e-7f?clamp((px*dx+py*dy)/length,0,1):0;
        float ex=px-t*dx,ey=py-t*dy,d2=ex*ex+ey*ey;
        if(d2>(r+.65f)*(r+.65f))continue;
        unsigned coverage=(unsigned)lroundf(alpha*clamp(r+.65f-sqrtf(d2),0,1));
        if(coverage)c->plot(c->context,x,y,colour,coverage>255?255:coverage);
    }
}
static void line(canvas_t *c,point_t a,point_t b,float width,uint16_t colour,unsigned alpha){
    if(!alpha)return;
    float dx=b.x-a.x,dy=b.y-a.y,length=dx*dx+dy*dy,r=width*.5f;
    int left=(int)floorf(maxf(0,minf(a.x,b.x)-r-1)),right=(int)ceilf(minf(480,maxf(a.x,b.x)+r+1));
    int top=(int)floorf(maxf(0,minf(a.y,b.y)-r-1)),bottom=(int)ceilf(minf(480,maxf(a.y,b.y)+r+1));
    for(int y=top;y<bottom;y++){
        int first=left;
        if(c->inside){
            int cut_left=c->inside[y][0],cut_right=c->inside[y][1];
            if(cut_left<cut_right&&first<cut_right&&right>cut_left){
                int end=right<cut_left?right:cut_left;
                if(first<end)line_row(c,y,first,end,a,dx,dy,length,r,colour,alpha);
                if(first<cut_right)first=cut_right;
            }
        }
        if(first<right)line_row(c,y,first,right,a,dx,dy,length,r,colour,alpha);
    }
}
static void arc(canvas_t *c,float x,float y,float rx,float ry,float begin,float sweep,
                 float width,uint16_t colour,unsigned alpha){
    unsigned steps=(unsigned)clamp(fabsf(sweep)*maxf(rx,ry)/7,10,48);
    point_t previous=point(x+rx*cosf(begin),y+ry*sinf(begin));
    for(unsigned i=1;i<=steps;i++){
        float angle=begin+sweep*i/steps;
        point_t next=point(x+rx*cosf(angle),y+ry*sinf(angle));
        line(c,previous,next,width,colour,alpha);previous=next;
    }
}
static void cloud(canvas_t *c,float x,float y,float width,float height,
                   uint16_t colour,unsigned alpha){
    /* Two long, shallow lobes make one readable cloud silhouette. */
    oval(c,x,y,width*.5f,height*.43f,colour,alpha);
    oval(c,x-width*.16f,y-height*.30f,width*.22f,height*.42f,colour,alpha);
}
static void ribbon(canvas_t *c,float x,float y,float length,float amplitude,float phase,
                    float width,uint16_t colour,unsigned alpha){
    point_t previous=point(x,y+amplitude*sinf(phase));
    for(unsigned i=1;i<=14;i++){
        float u=(float)i/14;point_t next=point(x+length*u,y+amplitude*sinf(phase+u*2*PI));
        float feather=sinf(PI*(u-.5f/14));
        line(c,previous,next,width,colour,(unsigned)(alpha*maxf(0,feather)));previous=next;
    }
}
static void leaf(canvas_t *c,point_t root,point_t tip,float width,uint16_t colour,unsigned alpha){
    float dx=tip.x-root.x,dy=tip.y-root.y,length=sqrtf(dx*dx+dy*dy);
    if(length<.01f)return;
    point_t normal=point(-dy/length*width,dx/length*width);
    point_t p[]={root,add(add(root,mul(point(dx,dy),.34f)),normal),tip,
                 add(add(root,mul(point(dx,dy),.59f)),mul(normal,-.55f))};
    polygon(c,p,4,colour,alpha);
}
static void frond(canvas_t *c,point_t root,float angle,float length,float phase,
                   uint16_t dark,uint16_t bright){
    point_t axis=point(cosf(angle),sinf(angle)),normal=point(-axis.y,axis.x);
    point_t tip=add(root,mul(axis,length));
    line(c,root,tip,2.1f,dark,215);
    for(unsigned i=0;i<6;i++){
        float u=.18f+i*.125f,reach=length*(.26f-.15f*u);
        point_t joint=add(root,mul(axis,length*u));
        for(unsigned side=0;side<2;side++){
            float sign=side?1.0f:-1.0f;
            point_t end=add(joint,add(mul(axis,reach*.82f),mul(normal,sign*reach*(.70f+.10f*sinf(phase+i)))));
            leaf(c,joint,end,4.5f+(1-u)*3.0f,side?bright:dark,190);
        }
    }
}
static void sunset(canvas_t *c,float t){
    const uint16_t cream=RGB(255,232,180),peach=RGB(246,174,113);
    for(unsigned i=0;i<3;i++){
        float x=fmodf(78+i*207+t*(9-i*2),680)-100;
        cloud(c,x,37+i*33,133-i*13,10+i*2,cream,75-i*12);
    }
    ribbon(c,-18+5*sinf(t*.4f),115,214,5,t*.85f,2,cream,105);
    ribbon(c,303,24,152,3,-t*.7f,1.4f,peach,110);
    for(unsigned i=0;i<4;i++){
        float y=388+i*19+4*sinf(t*1.1f+i),x=440+5*cosf(t*.7f+i);
        line(c,point(x-13,y),point(x+13+6*sinf(t+i),y-3),1.5f,cream,65+(unsigned)(30*(.5f+.5f*sinf(t*1.7f+i))));
    }
}
static void neon(canvas_t *c,float t){
    const uint16_t cyan=RGB(91,233,234),purple=RGB(172,137,251);
    arc(c,278,49,63,43,t*.58f,PI*1.35f,2.0f,purple,115);
    arc(c,278,49,77,52,-t*.43f+.8f,PI*.90f,1.3f,cyan,115);
    float sx=fmodf(t*34+18,530)-25;
    point_t beam[]={point(sx-12,0),point(sx+4,0),point(sx+50,134),point(sx+31,134)};
    polygon(c,beam,4,cyan,23);
    ribbon(c,18,113,186,8,t*1.25f,1.5f,cyan,135);
    for(unsigned i=0;i<3;i++){
        float a=t*(.46f+i*.12f)+i*2.1f,x=278+(63+i*7)*cosf(a),y=49+(43+i*4)*sinf(a);
        oval(c,x,y,2.2f,2.2f,cyan,220);
    }
    arc(c,455,427,21,13,t*.8f,PI*1.1f,1.7f,purple,130);
}
static void film(canvas_t *c,float t){
    const uint16_t light=RGB(255,239,196),caramel=RGB(185,119,77);
    float swing=17*sinf(t*.55f),flicker=.78f+.22f*sinf(t*2.1f);
    point_t beam[]={point(45,23),point(54,23),point(179+swing,119),point(89+swing,119)};
    polygon(c,beam,4,light,(unsigned)(62*flicker));
    float x=112+11*sinf(t*.65f),y=62+5*cosf(t*.55f);
    arc(c,x,y,44,22,t*.4f,PI*1.65f,2.0f,caramel,120);
    arc(c,280+8*cosf(t*.5f),57,38,21,-t*.42f,PI*1.35f,1.4f,caramel,125);
    ribbon(c,37,110,163,7,t*.85f,1.6f,caramel,110);
    for(unsigned i=0;i<5;i++){
        float y1=fmodf(30+i*99+t*15,530)-25;
        line(c,point(9,y1),point(15,y1-7),2.0f,light,105);
        line(c,point(465,505-y1),point(470,498-y1),1.8f,light,100);
    }
}
static void snow(canvas_t *c,float t){
    const uint16_t white=RGB(246,251,244),ice=RGB(201,228,236);
    for(unsigned i=0;i<2;i++){
        float x=fmodf(85+i*303+t*(8-i*3),670)-90;
        cloud(c,x,26+i*62,157,12,white,90-i*18);
    }
    ribbon(c,-32,100,222,6,t*1.2f,2.0f,white,150);
    ribbon(c,289,32,166,4,-t*.92f,1.4f,ice,150);
    ribbon(c,414,395,93,5,t,1.5f,white,115);
    for(unsigned i=0;i<34;i++){
        float depth=(float)(i%3),speed=7+depth*7;
        float x=fmodf((float)((i*137+31)%560)+t*(2+depth*3)+5*sinf(t*.55f+i),560)-40;
        float y=fmodf((float)((i*83+11)%560)+t*speed,560)-40;
        float r=.75f+depth*.42f;
        oval(c,x,y,r,r,white,100+(unsigned)(depth*55));
    }
}
static void forest(canvas_t *c,float t){
    const uint16_t leaf_dark=RGB(30,100,75),leaf_light=RGB(81,145,95),gold=RGB(234,249,162);
    float sweep=10*sinf(t*.45f);
    point_t a[]={point(247+sweep*.22f,28),point(257+sweep*.22f,28),point(205+sweep,170),point(179+sweep,170)};
    point_t b[]={point(269+sweep*.15f,29),point(275+sweep*.15f,29),point(320+sweep*.6f,166),point(305+sweep*.6f,166)};
    polygon(c,a,4,RGB(160,194,129),36);polygon(c,b,4,RGB(164,202,135),30);
    frond(c,point(-13,142),-.55f+.14f*sinf(t*1.05f),140,t,leaf_dark,leaf_light);
    frond(c,point(493,251),3.90f+.14f*sinf(t*.86f+.9f),140,t+.8f,leaf_dark,leaf_light);
    for(unsigned i=0;i<13;i++){
        float x=(float)((i*137+45)%480)+9*sinf(t*.7f+i*1.7f);
        float y=33+(float)((i*79+17)%425)+7*cosf(t*.8f+i);
        unsigned alpha=(unsigned)(65+170*powf(.5f+.5f*sinf(t*1.7f+i*2.3f),2));
        oval(c,x,y,2.4f,2.4f,RGB(177,220,119),alpha/4);
        oval(c,x,y,1.0f,1.0f,gold,alpha);
    }
}
static void lightning_path(canvas_t *c,const point_t *path,unsigned count,
                            float growth,unsigned alpha){
    if(!alpha||growth<=0)return;
    const uint16_t halo=RGB(126,165,235),ice=RGB(227,239,255);
    for(unsigned i=1;i<count;i++){
        float visible=clamp(growth*(count-1)-(i-1),0,1);
        if(visible<=0)break;
        point_t end=add(path[i-1],mul(add(path[i],mul(path[i-1],-1)),visible));
        line(c,path[i-1],end,5.3f,halo,alpha/3);
        line(c,path[i-1],end,1.65f,ice,alpha);
    }
}
static point_t strike_point(float x,float y,unsigned variant,float age){
    static const float origin_x[]={294,151,336,474};
    static const float origin_y[]={9,8,3,11};
    static const float scale_x[]={1,1.08f,.88f,.30f};
    static const float scale_y[]={1,1.12f,.94f,1.27f};
    float px=origin_x[variant]+x*scale_x[variant]+.8f*sinf(age*109+x*.31f+y*.17f);
    /* Right-edge strike stays clear of the upper-right control pill. */
    if(variant==3)px=maxf(456,px);
    return point(px,origin_y[variant]+y*scale_y[variant]);
}
static void storm(canvas_t *c,float t){
    const uint16_t rain=RGB(169,201,232);
    float clock=t+.18f,age=fmodf(clock,4.9f),energy=0;
    unsigned variant=(unsigned)floorf(clock/4.9f)%4;
    if(age<.11f)energy=.30f+.70f*age/.11f;
    else if(age<.23f)energy=1-.42f*(age-.11f)/.12f;
    else if(age<.29f)energy=.10f;
    else if(age<.37f)energy=clamp(1-fabsf(age-.325f)/.045f,0,1);
    else if(age<.70f){float fade=1-(age-.37f)/.33f;energy=.52f*fade*fade;}
    energy*=.78f+.22f*sinf(age*157+.8f);
    /* Illuminate the existing moving clouds in the same pass. No screen-sized
     * flash rectangle or extra cloud fill competes with the skating surface. */
    const uint16_t cloud_light=RGB(100+(unsigned)(75*energy),116+(unsigned)(70*energy),151+(unsigned)(60*energy));
    for(unsigned i=0;i<3;i++){
        float x=fmodf(65+i*213+t*(11-i*2),690)-100;
        cloud(c,x,26+i*29,177-i*13,13+i*2,cloud_light,70-i*10+(unsigned)(55*energy));
    }
    ribbon(c,285,119,198,4,-t*1.6f,1.6f,RGB(121,145,183),90);
    for(unsigned i=0;i<49;i++){
        float speed=88+(i%4)*16;
        float x=fmodf((float)((i*127+73)%600)-t*43+160000,600)-60;
        float y=fmodf((float)((i*67+29)%560)+t*speed,560)-40;
        line(c,point(x,y),point(x-4,y+8+(i%3)*2),.9f,rain,100+(i%3)*20);
    }
    unsigned alpha=(unsigned)(255*energy);
    if(alpha){
        static const point_t main_path[]={{0,0},{-17,25},{-5,21},{-31,59},{-16,52},{-43,102}};
        point_t path[6],branch_a[4],branch_b[4];
        for(unsigned i=0;i<6;i++)path[i]=strike_point(main_path[i].x,main_path[i].y,variant,age);
        /* Separate leaders grow out of already reached junctions. The second
         * bright return stroke reveals the completed branches, then each fades. */
        float side=variant==2?-1.0f:1.0f;
        branch_a[0]=path[2];
        branch_a[1]=strike_point(-5+side*22,36,variant,age);
        branch_a[2]=strike_point(-5+side*15,49,variant,age);
        branch_a[3]=strike_point(-5+side*31,64,variant,age);
        branch_b[0]=path[3];
        branch_b[1]=strike_point(-52,66,variant,age);
        branch_b[2]=strike_point(-43,81,variant,age);
        branch_b[3]=strike_point(-68,108,variant,age);
        lightning_path(c,path,6,clamp(age/.13f,0,1),alpha);
        lightning_path(c,branch_a,4,clamp((age-.055f)/.14f,0,1),alpha*4/5);
        lightning_path(c,branch_b,4,clamp((age-.12f)/.16f,0,1),alpha*3/5);
    }
}
void scene_ambience_draw(unsigned theme,uint64_t samples,scene_ambience_plot_t plot,void *context){
    scene_ambience_draw_clipped(theme,samples,plot,context,NULL);
}
void scene_ambience_draw_clipped(unsigned theme,uint64_t samples,scene_ambience_plot_t plot,void *context,
                                  const uint16_t (*inside)[2]){
    if(!plot||theme<1||theme>6)return;
    canvas_t canvas={plot,context,inside};
    float t=(float)(samples%(16000ull*3600))/16000.0f;
    switch(theme){
        case 1:sunset(&canvas,t);break;
        case 2:neon(&canvas,t);break;
        case 3:film(&canvas,t);break;
        case 4:snow(&canvas,t);break;
        case 5:forest(&canvas,t);break;
        case 6:storm(&canvas,t);break;
        default:break;
    }
}
