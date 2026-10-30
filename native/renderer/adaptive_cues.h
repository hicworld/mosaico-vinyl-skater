/* Motion revision 3: individually authored anime performances retain the
 * production foot-contact clock. Lift fractions are actual
 * quantized sprite frames, not guessed musical phases. A monotone whole-pose
 * time warp can retime these events without modifying any foot or arm. */
#pragma once
#include <stdint.h>
#define MOTION_SPIN_BEGIN .24f
#define MOTION_SPIN_END .78f
#define MOTION_SPIN_RAMP .16f
static inline float motion_spin_progress(float phase){
    float x=(phase-MOTION_SPIN_BEGIN)/(MOTION_SPIN_END-MOTION_SPIN_BEGIN);
    if(x<=0)return 0;
    if(x>=1)return 1;
    float ramp=MOTION_SPIN_RAMP;
    if(x<ramp){float t=x/ramp;return ramp*t*t*t*t*(2.5f-3*t+t*t)/(1-ramp);}
    if(x>1-ramp){float t=(1-x)/ramp;return 1-ramp*t*t*t*t*(2.5f-3*t+t*t)/(1-ramp);}
    return (x-.5f*ramp)/(1-ramp);
}
enum { MOTION_CUE_LIFT=1, MOTION_CUE_CONTACT_LOCK=2, MOTION_CUE_FLIGHT=3,
       MOTION_CUES_MAX=8 };
typedef struct { float phase; uint8_t feet,kind; } motion_cue_t;
typedef struct { uint8_t count; motion_cue_t cues[MOTION_CUES_MAX]; } motion_cue_clip_t;
static const motion_cue_clip_t motion_cue_clips[19] = {
    {0,{{0.0f,0,0}}}, /* 0 cruise: grounded */
    {2,{{12.0f/39,1,MOTION_CUE_LIFT},{29.0f/39,2,MOTION_CUE_LIFT}}},
    {0,{{0.0f,0,0}}}, /* 2 slalom: grounded */
    {0,{{0.0f,0,0}}}, /* 3 legacy dip */
    {0,{{0.0f,0,0}}}, /* 4 legacy spread */
    {0,{{0.0f,0,0}}}, /* 5 held balance: preserve the long support transfer */
    {0,{{0.0f,0,0}}}, /* 6 held balance */
    {0,{{0.0f,0,0}}}, /* 7 grounded scissors */
    {3,{{17.0f/48,2,MOTION_CUE_LIFT},{.5f,2,MOTION_CUE_CONTACT_LOCK},
        {31.0f/48,2,MOTION_CUE_LIFT}}}, /* keep the toe touch on its beat */
    {0,{{0.0f,0,0}}}, /* 9 arm sweep */
    {4,{{11.0f/87,1,MOTION_CUE_LIFT},{33.0f/87,1,MOTION_CUE_LIFT},
        {54.0f/87,2,MOTION_CUE_LIFT},{76.0f/87,2,MOTION_CUE_LIFT}}},
    {0,{{0.0f,0,0}}}, /* 11 Crazy has no airborne wheel: never fake a foot lift */
    {0,{{0.0f,0,0}}}, /* 12 grounded eagle */
    {0,{{0.0f,0,0}}}, /* 13 legacy heel/toe */
    {4,{{.28f,3,MOTION_CUE_CONTACT_LOCK},{.5f,3,MOTION_CUE_FLIGHT},
        {.72f,1,MOTION_CUE_CONTACT_LOCK},{.745f,2,MOTION_CUE_CONTACT_LOCK}}},
    {0,{{0.0f,0,0}}}, /* 15 held one-foot swivel */
    {4,{{14.0f/112,1,MOTION_CUE_LIFT},{42.0f/112,2,MOTION_CUE_LIFT},
        {70.0f/112,1,MOTION_CUE_LIFT},{98.0f/112,2,MOTION_CUE_LIFT}}},
    {0,{{0.0f,0,0}}}, /* 17 keep the continuous pivot and support transfer intact */
    {4,{{.39f,3,MOTION_CUE_CONTACT_LOCK},{.5f,3,MOTION_CUE_FLIGHT},
        {.61f,1,MOTION_CUE_CONTACT_LOCK},{.6225f,2,MOTION_CUE_CONTACT_LOCK}}},
};
#include "slalom_routes.h"
static inline const motion_cue_clip_t *motion_clip_cues(unsigned clip){
    return clip<19?&motion_cue_clips[clip]:&motion_slalom_cues[clip-19];
}
static inline unsigned motion_clip_flight(unsigned clip){
    return clip==14||clip==18||(clip>=19&&clip<55&&motion_slalom_routes[clip-19].flight);
}
