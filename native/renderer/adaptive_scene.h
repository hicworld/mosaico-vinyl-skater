#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "adaptive_motion.h"
#if SCENE_PC_SMOOTH_CHARACTERS
#include "neon_character.h"
bool adaptive_scene_secondary_motion(const motion_timeline_t *timeline,
                                      uint64_t samples,
                                      skating_secondary_motion_t *motion);
#endif
#define SCENE_WIDTH 480u
#define SCENE_HEIGHT 480u
#define SCENE_TILES 225u
#define SCENE_SKINS 4u
#define SCENE_THEMES SONG_ACTIVE_THEME_COUNT
#define SCENE_EFFECTS SONG_EFFECT_COUNT
#define SCENE_RECORD_COLOR_COUNT 10u
#define SCENE_SCENE_RECORD_BASE 4u
#define SCENE_SCENE_RECORD_COUNT 6u
#ifndef SCENE_SCRATCH_BYTES
#define SCENE_SCRATCH_BYTES 196608u
#endif
#define SCENE_MAIN_SCRATCH_BYTES 55296u
#define SCENE_WORKER_SCRATCH_BYTES (SCENE_SCRATCH_BYTES-SCENE_MAIN_SCRATCH_BYTES)
/* The authored bank remains at 2.7, while the displayed body returns to
 * the earlier 1.8 * 1.28 footprint. Sampling quality is independent of size. */
#define SCENE_CHARACTER_SCALE 0.853333333f
#define SCENE_TRAIL_POINTS 97u
/* Contact tolerance follows the authored model's 1.8 -> 2.7 world scale.
 * It preserves the existing contact frames instead of delaying touchdown. */
#define SCENE_CONTACT_HEIGHT .12f
/* Optional native whole-environment accelerator; both images are RGB565,
 * 480x480. Source is packed, canvas uses the caller-provided pixel stride.
 * Failure must leave canvas intact so the exact portable path can finish. */
typedef bool (*adaptive_scene_environment_blend_fn)(void *context,const uint16_t *source,
                                                   uint16_t *canvas,unsigned stride,unsigned alpha);

typedef bool (*adaptive_scene_environment_copy_fn)(void *context,const uint16_t *source,
                                                  unsigned source_stride,uint16_t *canvas,unsigned canvas_stride);

/* A native worker owns its storage independently of the scene scratch. A
 * successful collect lends these RGB565-low/high/alpha bytes until this
 * frame's character composition finishes, before the next begin call. */
typedef struct {
    const uint8_t *pixels;
    int left,top;
    unsigned width,height;
} adaptive_scene_neon_result_t;
typedef bool (*adaptive_scene_neon_begin_fn)(void *context,unsigned frame0,unsigned frame1,
                                            float blend,float scale,uint32_t *token);
typedef bool (*adaptive_scene_neon_collect_fn)(void *context,uint32_t token,
                                              adaptive_scene_neon_result_t *result);

typedef struct {
    uint32_t trail_setup_us,trail_raster_us,trail_composite_us;
    uint32_t trail_clear_bytes,trail_raster_tests,trail_segment_count,restore_tiles;
    uint32_t paper_cache_hits,paper_cache_misses;
    uint32_t environment_us,details_us,ambience_us,trails_us;
    uint32_t effects_us,character_us,controls_us,total_us;
    uint32_t motion_evaluations,trail_cache_hits,landing_cache_hits;
} adaptive_scene_profile_t;

typedef struct {
    uint64_t sample;
    motion_pose_t pose;
    unsigned frame,contact_mask;
    float u[2],v[2],contact[2],pressure[2],base;
} adaptive_scene_motion_cache_t;

typedef struct {
    uint16_t *canvas,*background;
    /* Legacy diagnostic pointer; alwaysNULL. No snapshot is retained or read. */
    uint16_t *transition_background;
    adaptive_scene_environment_blend_fn environment_blend;
    void *environment_blend_context;
    adaptive_scene_environment_copy_fn environment_copy;
    void *environment_copy_context;
    bool dense_copy_enabled;
    adaptive_scene_neon_begin_fn neon_begin;
    adaptive_scene_neon_collect_fn neon_collect;
    void *neon_context;
    uint32_t transition_now_ms,transition_start_ms,transition_duration_ms;
    unsigned transition_alpha,transition_count,transition_source_theme;
    bool transition_automatic,transition_active;
    uint8_t *scratch,*neon_scratch,*trail_alpha;
    size_t main_capacity,neon_capacity;
    bool scratch_partitioned;
    unsigned stride;
    adaptive_scene_profile_t profile;
    /* The 96 old deposits use an absolute audio lattice. Cache their exact
     * evaluator result; the live tip is still evaluated at every new sample. */
    motion_timeline_t motion_cache_timeline;
    bool motion_cache_valid;
    adaptive_scene_motion_cache_t motion_cache[SCENE_TRAIL_POINTS-1];
    struct {unsigned plan,frame;uint32_t sample;} landing_sample_cache[16];
    /* Exact scene-ellipse clipping limits for the 480 integer scanlines. */
    uint16_t scenery_inside[SCENE_HEIGHT][2];
    /* Sparse alpha ranges are reset before the next musical frame. A row
     * outside these bounds is always zero after the first trail render. */
    uint16_t trail_row_left[SCENE_HEIGHT],trail_row_right[SCENE_HEIGHT];
    /* Conservative32-pixel blocks retain holes between separate marks. The
     * old mask is consumed before this frame starts depositing new alpha. */
    uint16_t trail_row_mask[SCENE_HEIGHT];
    bool trail_rows_valid;
    uint8_t occupied[SCENE_TILES],prepared[SCENE_TILES],dirty[SCENE_TILES];
    uint64_t last_samples;
    unsigned last_skin,last_song,last_playing,last_theme,last_record_id,last_effect_id,clip;
    float phase;
    unsigned sprite;
    /* Sample-derived diagnostics, also used by the production WASI tests. */
    unsigned landing_events,splash_particles,splash_pixels,trail_air_samples;
    unsigned trail_current_mask;
    unsigned splash_sum_x,splash_sum_y,splash_alpha_sum;
    float audio_level;
    unsigned effect_mode,effect_pixels,effect_back_pixels,effect_front_pixels,effect_contact_mask;
    bool ambience_enabled,last_ambience_enabled;
    unsigned ambience_pixels;
    /* Exact opaque label coverage, derived once from the frozen art mask. */
    uint8_t label_opaque_span[144][2];
    bool label_opaque_valid;
    int16_t paper_red[32],paper_green[64],paper_blue[32];
    int paper_bias;
    uint16_t paper_ramp[256];
    float effect_drum_pulse;
    float feet[8]; /* Actual displayed, enlarged foot coordinates and loads. */
    float trail_min_width,trail_max_width,landing_u,landing_v,landing_age;
    float trail_fresh[SCENE_TRAIL_POINTS],trail_taper[SCENE_TRAIL_POINTS],trail_fade[SCENE_TRAIL_POINTS];
} adaptive_scene_t;
bool adaptive_scene_init(adaptive_scene_t *scene,uint16_t *canvas,unsigned stride,
                         uint16_t *background,uint8_t *scratch,size_t scratch_bytes,
                         uint8_t *trail_alpha);
bool adaptive_scene_render(adaptive_scene_t *scene,const motion_timeline_t *timeline,
                           uint64_t samples,unsigned skin,unsigned song,bool playing);

/* Configure once, before installing native worker callbacks, under the scene
 * ownership lock. main_scratch must be the existing scene workspace; its full
 * original allocation remains reserved for the fixed prefix/worker suffix.
 * The fallback must be a separate 128-byte-aligned allocation of at least
 * SCENE_SCRATCH_BYTES. Failure leaves every scene field unchanged. Neither
 * region may be rebound/freed while a native worker can still access it. */
bool adaptive_scene_configure_scratch(adaptive_scene_t *scene,uint8_t *main_scratch,
                                      size_t main_bytes,uint8_t *neon_fallback,
                                      size_t neon_bytes);

/* Legacy compatibility only: no snapshot is needed, retained, read or freed.
 * NULL/0 arguments are accepted. Scene and record changes appear immediately;
 * diagnostics remain active=false, alpha=255, duration/count=0. */
bool adaptive_scene_transition_init(adaptive_scene_t *scene,uint16_t *environment_snapshot,
                                    size_t pixel_count,unsigned duration_ms);
/* Retains the wall-clock/automatic diagnostic values without enabling fades. */
void adaptive_scene_transition_clock(adaptive_scene_t *scene,uint32_t now_ms,bool automatic);

/* Call under the timeline ownership lock before replacing metadata in place.
 * Pointer/count changes and sample rollback also invalidate automatically. */
void adaptive_scene_motion_cache_reset(adaptive_scene_t *scene);

void adaptive_scene_set_environment_blend(adaptive_scene_t *scene,
                                         adaptive_scene_environment_blend_fn blend,void *context);

/* A failed acceleration request falls back to copying all480 scanlines. */
void adaptive_scene_set_environment_copy(adaptive_scene_t *scene,
                                        adaptive_scene_environment_copy_fn copy,void *context);

/* Only controls dense occupied-tile restoration. Background rebuilds may
 * still use environment_copy. Disabled by default. */
void adaptive_scene_set_dense_copy(adaptive_scene_t *scene,bool enabled);

/* Configure under the scene ownership lock. Both callbacks are required.
 * begin must return promptly; collect implements its own bounded wait policy.
 * Rejection, timeout, or invalid output uses the exact synchronous renderer.
 * A timed-out job must never access the scene scratch or reuse a lent result
 * before the next accepted begin. Callbacks have one animation-task caller. */
void adaptive_scene_set_neon_async(adaptive_scene_t *scene,adaptive_scene_neon_begin_fn begin,
                                   adaptive_scene_neon_collect_fn collect,void *context);
