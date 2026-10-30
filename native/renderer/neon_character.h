#ifndef NEON_CHARACTER_H
#define NEON_CHARACTER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* This workspace also accommodates the largest existing sprite decode. The
 * returned pixels occupy its beginning, in RGB565 little endian + alpha8.
 * x/y are pixel offsets from the projected character anchor. */
#ifndef NEON_SCRATCH_BYTES
#define NEON_SCRATCH_BYTES (192u * 1024u)
#endif
bool neon_character_draw(unsigned frame, uint8_t *scratch, size_t bytes,
                         int *x, int *y, unsigned *w, unsigned *h);

/* Rig indices must belong to the same clip. Fractional sampling removes the
 * nearest-frame holds during turns; scale enlarges the whole rig, skates and
 * shadow around the same ground anchor. blend is clamped to [0,1], scale must
 * be finite and in [0.1,2]. No persistent pose state is needed for seeking. */
bool neon_character_draw_interpolated(unsigned frame0, unsigned frame1,
                                      float blend, float scale,
                                      uint8_t *scratch, size_t bytes,
                                      int *x, int *y, unsigned *w, unsigned *h);

/* Desktop continuous characters share the solved skeleton, not rendered
 * images. Fractional anchor offsets are applied before rasterization, so
 * slow gliding does not snap the whole character to integer pixels. The
 * legacy Neon entry point remains skin 3 with a zero fractional offset. */
bool skating_character_draw_interpolated(unsigned frame0, unsigned frame1,
                                         float blend, float scale, unsigned skin,
                                         float anchor_x, float anchor_y,
                                         uint8_t *scratch, size_t bytes,
                                         int *x, int *y, unsigned *w, unsigned *h);

/* Desktop attachment dynamics are reconstructed from an evenly spaced PCM
 * history. No wall clock or persistent simulation state participates, so the
 * same sample is identical after a seek, pause, or change of preview FPS. */
typedef struct {
    unsigned frame0, frame1;
    float blend, u, v;
} skating_motion_sample_t;
typedef struct {
    float hair_side, hair_back, hair_lift, hair_flare;
    float hem_side, hem_back, hem_lift, hem_flare;
    float twist, jacket_sway;
} skating_secondary_motion_t;
bool skating_character_secondary_motion(const skating_motion_sample_t *history,
                                         unsigned count, float step_seconds,
                                         skating_secondary_motion_t *out);
bool skating_character_draw_with_secondary(unsigned frame0, unsigned frame1,
                                            float blend, float scale, unsigned skin,
                                            float anchor_x, float anchor_y,
                                            const skating_secondary_motion_t *motion,
                                            uint8_t *scratch, size_t bytes,
                                            int *x, int *y, unsigned *w, unsigned *h);

#endif
