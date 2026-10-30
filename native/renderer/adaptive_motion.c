#include "adaptive_motion.h"
#include "adaptive_cues.h"
#include <math.h>
#define PI 3.14159265358979323846f
static float clamp(float x,float a,float b){return x<a?a:(x>b?b:x);}
static float smooth(float x){x=clamp(x,0,1);return x*x*x*(x*(6*x-15)+10);}
/* Cut quickly through the centre, then settle briefly into the edge. The
 * original zeroes, peaks and amplitude stay fixed, so authored push/lift cues
 * keep their musical position and stronger posing cannot enlarge the route. */
static float cut_wave(float angle){float s=sinf(angle);return s*(1.5f-.5f*s*s);}

/* Monotone cubic phase reaches each observed beat exactly, while avoiding
 * changes in route velocity when neighbouring beat intervals differ. */
static float beat_position(const motion_timeline_t *t,uint32_t sample){
    if(!t->beat_count)return 0;
    if(t->beat_count==1||sample<=t->beats[0].sample)return 0;
    unsigned lo=0,hi=t->beat_count-1;
    if(sample>=t->beats[hi].sample)return (float)hi;
    while(lo+1<hi){unsigned m=lo+(hi-lo)/2;if(t->beats[m].sample<=sample)lo=m;else hi=m;}
    float span=(float)(t->beats[lo+1].sample-t->beats[lo].sample);
    if(span<=0)return (float)lo;
    float before=lo?(float)(t->beats[lo].sample-t->beats[lo-1].sample):span;
    float after=lo+2<t->beat_count?(float)(t->beats[lo+2].sample-t->beats[lo+1].sample):span;
    float a=2*span/(before+span),b=2*span/(span+after),norm=a*a+b*b;
    if(norm>9){float scale=3/sqrtf(norm);a*=scale;b*=scale;}
    float f=(sample-t->beats[lo].sample)/span,f2=f*f,f3=f2*f;
    return lo+clamp((-2*f3+3*f2)+(f3-2*f2+f)*a+(f3-f2)*b,0,1);
}

static unsigned drum_lower_bound(const song_audio_features_t *audio,uint32_t sample){
    unsigned lo=0,hi=audio->drum_count;
    while(lo<hi){unsigned mid=lo+(hi-lo)/2;
        if(audio->drums[mid].sample<sample)lo=mid+1;else hi=mid;}
    return lo;
}

/* Retiming complete sprite poses keeps support feet, knees and arms exactly
 * as authored. Matching only genuine drum events also preserves offbeats.
 * The cue count and candidate count are bounded, independent of song length;
 * this evaluator is called for the visible actor and historical trail marks.
 * It has no mutable simulation/cache state, so seeking and pausing agree. */
static float reactive_phase(const motion_timeline_t *t,const song_plan_t *plan,float phase){
    if(!t->audio.drums||!t->audio.drum_count||plan->clip>=SONG_CLIP_COUNT||
       plan->first_beat==SONG_LINEAR_FIRST_BEAT||!plan->beat_count||phase<=0||phase>=1)return phase;
    /* Sustained pivots use the musical beat clock directly. Retiming their
     * free-leg lift apex toward individual drums used to stretch one yaw
     * sector and squeeze the next, making an otherwise uniform revolution
     * appear to linger. The other clips retain their foot/drum matching. */
    if(plan->clip==17||plan->clip==45)return phase;
    const motion_cue_clip_t *clip=motion_clip_cues(plan->clip);
    unsigned count=clip->count;if(!count)return phase;
    bool flight=motion_clip_flight(plan->clip);
    if(flight){
        /* Keep the authored push-off and landing compression in musical
         * time. Only the airborne apex may move slightly toward a drum. */
        float first=1,last=0;
        for(unsigned i=0;i<count;i++)if(clip->cues[i].kind==MOTION_CUE_CONTACT_LOCK){
            first=fminf(first,clip->cues[i].phase);last=fmaxf(last,clip->cues[i].phase);
        }
        if(first>=last||phase<=first||phase>=last)return phase;
        if(plan->clip>=19){
            /* A Crab / X jump phrase has two separate flights. Each apex
             * is bracketed by its own last ground frame and first landing
             * frame. Recovery and the second preparation stay exactly on
             * authored musical time, including drums between the hops. */
            bool airborne_window=false;
            for(unsigned i=0;i<count;i++)if(clip->cues[i].kind==MOTION_CUE_FLIGHT){
                float begin=0,end=1;
                for(unsigned k=i;k>0;k--)if(clip->cues[k-1].kind==MOTION_CUE_CONTACT_LOCK){begin=clip->cues[k-1].phase;break;}
                for(unsigned k=i+1;k<count;k++)if(clip->cues[k].kind==MOTION_CUE_CONTACT_LOCK){end=clip->cues[k].phase;break;}
                if(phase>begin&&phase<end)airborne_window=true;
            }
            if(!airborne_window)return phase;
        }
    }
    float x[MOTION_CUES_MAX+2]={0},y[MOTION_CUES_MAX+2]={0},m[MOTION_CUES_MAX+2];
    for(unsigned i=0;i<count;i++)x[i+1]=y[i+1]=clip->cues[i].phase;
    x[count+1]=y[count+1]=1;
    bool changed=false;uint32_t used[MOTION_CUES_MAX]={0};unsigned used_count=0;
    for(unsigned i=1;i<=count;i++){
        if(clip->cues[i-1].kind==MOTION_CUE_CONTACT_LOCK)continue;
        /* Limit phase movement and each local speed ratio. These bounds
         * leave preparation and recovery time, and prohibit repeated or
         * reversed anchors even with extremely dense / irregular drums. */
        float shift=flight?fminf(.04f,.25f/plan->beat_count):fminf(.22f,.75f/plan->beat_count);
        float slow=flight?.75f:.45f,fast=flight?1.35f:2.2f;
        float low=fmaxf(y[i]-shift,x[i-1]+(y[i]-y[i-1])/fast);
        low=fmaxf(low,x[i+1]-(y[i+1]-y[i])/slow);
        float high=fminf(y[i]+shift,x[i-1]+(y[i]-y[i-1])/slow);
        high=fminf(high,x[i+1]-(y[i+1]-y[i])/fast);
        if(low>=high)continue;
        float target_beat=plan->first_beat+y[i]*plan->beat_count;
        unsigned b=(unsigned)floorf(target_beat);
        if(b+1>=t->beat_count)continue;
        float fraction=target_beat-b;
        uint32_t target=t->beats[b].sample+(uint32_t)lroundf(
            fraction*(t->beats[b+1].sample-t->beats[b].sample));
        unsigned nearest=drum_lower_bound(&t->audio,target);
        unsigned first=nearest>4?nearest-4:0,last=nearest+4;
        if(last>t->audio.drum_count)last=t->audio.drum_count;
        float best_score=100,best_phase=x[i];uint32_t best_sample=0;bool found=false;
        for(unsigned d=first;d<last;d++){
            const song_drum_t *drum=&t->audio.drums[d];
            if(drum->kind!=1||!drum->strength||drum->sample<=plan->start_sample||drum->sample>=plan->end_sample)continue;
            bool duplicate=false;
            for(unsigned k=0;k<used_count;k++)if(used[k]==drum->sample)duplicate=true;
            if(duplicate)continue;
            float candidate=(beat_position(t,drum->sample)-plan->first_beat)/plan->beat_count;
            if(candidate<low||candidate>high)continue;
            float score=fabsf(candidate-y[i])-.018f*(drum->strength/65535.0f);
            if(score<best_score){best_score=score;best_phase=candidate;best_sample=drum->sample;found=true;}
        }
        if(found){x[i]=best_phase;used[used_count++]=best_sample;
            if(fabsf(x[i]-y[i])>1.e-7f)changed=true;}
    }
    if(!changed)return phase; /* exact legacy output when no cue can match */
    float slopes[MOTION_CUES_MAX+1];
    for(unsigned i=0;i<=count;i++)slopes[i]=(y[i+1]-y[i])/(x[i+1]-x[i]);
    m[0]=m[count+1]=1; /* preserve the authored neutral entry/exit tangent */
    for(unsigned i=1;i<=count;i++){
        m[i]=fminf(.5f*(slopes[i-1]+slopes[i]),1.35f*fminf(slopes[i-1],slopes[i]));
        if(flight&&clip->cues[i-1].kind==MOTION_CUE_CONTACT_LOCK)m[i]=1;
    }
    for(unsigned i=0;i<=count;i++)if(phase<=x[i+1]){
        float h=x[i+1]-x[i],u=(phase-x[i])/h,u2=u*u,u3=u2*u;
        return clamp((2*u3-3*u2+1)*y[i]+(u3-2*u2+u)*h*m[i]
                   +(-2*u3+3*u2)*y[i+1]+(u3-u2)*h*m[i+1],y[i],y[i+1]);
    }
    return phase;
}
static float carrier_radius(const motion_timeline_t *t,float beat){
    float envelope=smooth(beat/4)*smooth(((float)t->beat_count-1-beat)/4);
    return 128+16*(1-cosf(2*PI*beat/32))*envelope;
}
bool motion_evaluate(const motion_timeline_t *t,uint64_t samples,motion_pose_t *q){
    if(!t||!q||!t->sample_count||!t->plans||!t->plan_count||!t->beats||!t->beat_count)return false;
    uint32_t position=(uint32_t)(samples%t->sample_count);
    unsigned p=song_plan_find(t->plans,t->plan_count,position);
    if(p>=t->plan_count)return false;
    const song_plan_t *plan=&t->plans[p];
    if(plan->clip>=SONG_CLIP_COUNT||plan->end_sample<=plan->start_sample)return false;
    float beat=beat_position(t,position),phase;
    if(plan->first_beat==SONG_LINEAR_FIRST_BEAT&&plan->beat_count==0){
        phase=(float)(position-plan->start_sample)/(plan->end_sample-plan->start_sample);
    }else{
        if(!plan->beat_count||(uint32_t)plan->first_beat+plan->beat_count>=t->beat_count)return false;
        phase=(beat-plan->first_beat)/plan->beat_count;
    }
    phase=clamp(phase,0,1);
    phase=reactive_phase(t,plan,phase);
    /* Intro and ending converge to the same position, allowing song looping.
     * Local cut-ins return to zero at clip boundaries with zero tangent. */
    float radius=carrier_radius(t,beat);
    float clip_envelope=smooth(phase/.16f)*smooth((1-phase)/.16f);
    float crest=sinf(PI*phase);crest*=crest;
    float strength=.65f+.35f*(plan->energy/65535.0f);
    /* Each musical phrase draws its own skate line. Every offset and its
     * tangent vanish at the clip endpoints, keeping a grounded hand-off. */
    switch(plan->clip){
        case 1:radius-=8*crest*strength;break;       /* paired driving pushes */
        case 2:radius+=8*cut_wave(2*PI*phase)*clip_envelope*strength;break;
        case 3:radius-=8*crest*strength;break;       /* low inward glide */
        case 4:radius+=14*crest*strength;break;      /* broad open-arm arc */
        case 5:radius-=8*crest*strength;break;       /* held inner balance */
        case 6:radius+=10*crest*strength;break;      /* held outer balance */
        case 7:radius+=7*cut_wave(4*PI*phase)*clip_envelope*strength;break;
        case 8:radius-=3*crest*strength;break;       /* grounded toe tap */
        case 9:radius+=8*crest*strength;break;       /* sweeping arm phrase */
        case 10:radius-=7*crest*strength;break;     /* crossing drive steps */
        case 11:radius+=8*cut_wave(4*PI*phase)*clip_envelope*strength;break;
        case 12:radius+=12*crest*strength;break;    /* wide turned-out eagle */
        case 13:radius+=5*sinf(2*PI*phase)*clip_envelope*strength;break;
        case 14:radius-=8*crest*strength;break;      /* travelling aerial 360 */
        case 15:radius-=10*crest*strength;break;    /* single-foot pivot */
        case 16:radius+=24*cut_wave(4*PI*phase)*clip_envelope*strength;break;
        case 17:break;                            /* planted local pivot below */
        case 18:radius-=18*crest*strength;break;    /* longer aerial 720 arc */
        default:break;
    }
    const float theta=.952949772f;
    float radial=0,tangent=0;
    if(plan->clip==16){
        /* The carrier record supplies forward travel; these alternating
         * inner / outer cuts add real lateral displacement to the wheels. */
        tangent=12*cut_wave(2*PI*phase)*clip_envelope*strength;
    }else if(plan->clip==17){
        /* Enter on a short inner arc, turn around the rig's actual loaded
         * front wheel, then leave the arc. Freeze the broad carrier-radius
         * drift during the pivot so it cannot drag the planted foot sideways.
         * The small oval shares the rig's spin progress exactly; the body
         * itself supplies the two rotations, with no whole-record orbit. */
        float entering=smooth(phase/MOTION_SPIN_BEGIN);
        float leaving=smooth((1-phase)/(1-MOTION_SPIN_END));
        float hold=entering*leaving;
        float pivot=carrier_radius(t,plan->first_beat+MOTION_SPIN_BEGIN*plan->beat_count)-12*strength;
        radial=(pivot-radius)*hold;tangent=-5*hold*strength;
        if(phase<MOTION_SPIN_BEGIN){
            float arc=sinf(PI*phase/MOTION_SPIN_BEGIN);tangent+=3*arc*arc*strength;
        }else if(phase>MOTION_SPIN_END){
            float arc=sinf(PI*(phase-MOTION_SPIN_END)/(1-MOTION_SPIN_END));tangent-=2*arc*arc*strength;
        }
        /* Evaluate the return with a small mirrored progress to retain
         * float32 precision at the neutral exit after two full rotations. */
        float middle=(MOTION_SPIN_BEGIN+MOTION_SPIN_END)*.5f;
        float angle=phase>middle?-4*PI*motion_spin_progress(MOTION_SPIN_BEGIN+MOTION_SPIN_END-phase)
                                :4*PI*motion_spin_progress(phase);
        radial+=3*(cosf(angle)-1)*strength;tangent+=2.5f*sinf(angle)*strength;
    }else if(plan->clip==14||plan->clip==18){
        tangent=(plan->clip==18?17:9)*sinf(2*PI*phase)*clip_envelope*strength;
    }
    if(plan->clip>=19){
        const slalom_route_t *route=&motion_slalom_routes[plan->clip-19];
        /* New references each have an authored route profile. Grounded
         * eight / Crazy phrases stay around one local centre; travelling
         * phrases cut across the record with the same normalized clock as
         * their feet. All offsets return with zero position and tangent at
         * a phrase join, and seeking never depends on previous frames. */
        float route_phase=clamp((phase-.12f)/.76f,0,1);
        float wave=cut_wave(2*PI*route->cycles*route_phase);
        if(route->local){
            float centre_beat=plan->first_beat==SONG_LINEAR_FIRST_BEAT?beat:plan->first_beat+.5f*plan->beat_count;
            float centre=carrier_radius(t,centre_beat);
            radial+=(centre-radius)*clip_envelope;
        }
        radial+=route->radial*wave*clip_envelope*strength;
        tangent+=route->lateral*cut_wave(2*PI*route_phase)*clip_envelope*strength;
        tangent+=route->travel*sinf(PI*route_phase)*sinf(PI*route_phase)*strength;
        if(route->eight){
            radial+=route->eight*sinf(2*PI*route_phase)*clip_envelope*strength;
            tangent+=route->eight*.62f*sinf(4*PI*route_phase)*clip_envelope*strength;
        }
    }
    float u=(radius+radial)*cosf(theta)-tangent*sinf(theta);
    float v=(radius+radial)*sinf(theta)+tangent*cosf(theta);
    *q=(motion_pose_t){.clip=plan->clip,.plan=p,.phase=phase,.beat_position=beat,
        .beat_phase=beat-floorf(beat),.energy=plan->energy/65535.0f,
        .radius=sqrtf(u*u+v*v),.u=u,.v=v};
    return true;
}
