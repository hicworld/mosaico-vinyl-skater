#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "song_format.h"

typedef struct {
    uint32_t sample_count,beat_count,plan_count;
    const song_beat_t *beats;
    const song_plan_t *plans;
    song_audio_features_t audio;
    unsigned theme;
    unsigned record_id;
    unsigned effect_id;
} motion_timeline_t;
typedef struct {
    unsigned clip,plan;
    float phase,beat_position,beat_phase,energy,radius,u,v;
} motion_pose_t;
bool motion_evaluate(const motion_timeline_t *timeline,uint64_t samples,motion_pose_t *pose);
