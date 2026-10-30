#ifndef SCENE_AMBIENCE_H
#define SCENE_AMBIENCE_H

#include <stdint.h>

typedef void (*scene_ambience_plot_t)(void *context, int x, int y,
                                      uint16_t colour, unsigned alpha);

/* Original procedural scenery for public scene IDs1..6. Every position and
 * opacity comes directly from absolute16kHz PCM samples; no mutable animation
 * state is used. The caller clips the vinyl in plot, then composites controls.
 * Invalid themes and a null callback draw nothing. No image assets are used. */
void scene_ambience_draw(unsigned theme, uint64_t samples,
                         scene_ambience_plot_t plot, void *context);

/* Optionally skip the hidden half-open x interval in each of the 480 rows.
 * Accepted pixels keep the original drawing order, colour and coverage.
 * Empty/reversed intervals exclude nothing; NULL keeps the original API. */
void scene_ambience_draw_clipped(unsigned theme, uint64_t samples,
                                 scene_ambience_plot_t plot, void *context,
                                 const uint16_t (*inside)[2]);

#endif
