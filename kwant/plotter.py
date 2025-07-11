# -*- coding: utf-8 -*-
# Copyright 2011-2018 Kwant authors.
#
# This file is part of Kwant.  It is subject to the license terms in the file
# LICENSE.rst found in the top-level directory of this distribution and at
# https://kwant-project.org/license.  A list of Kwant authors can be found in
# the file AUTHORS.rst at the top-level directory of this distribution and at
# https://kwant-project.org/authors.

"""Plotter module for Kwant.

This module provides iterators useful for any plotter routine, such as a list
of system sites, their coordinates, lead sites at any lead unit cell, etc.  If
`matplotlib` is available, it also provides simple functions for plotting the
system in two or three dimensions.
"""

from collections import defaultdict
import itertools
import functools
import warnings
import cmath
import numpy as np
import tinyarray as ta
from scipy import spatial, interpolate
from math import cos, sin, pi, sqrt

from . import system, builder, _common
from ._common import deprecate_args


__all__ = ['set_engine', 'get_engine',
           'plot', 'map', 'bands', 'spectrum', 'current', 'density',
           'interpolate_current', 'interpolate_density',
           'streamplot', 'scalarplot',
           'sys_leads_sites', 'sys_leads_hoppings', 'sys_leads_pos',
           'sys_leads_hopping_pos', 'mask_interpolate']

# All the expensive imports are done in _plotter.py. We lazy load the module
# to avoid slowing down the initial import of Kwant.
_p = _common.lazy_import('_plotter')


def set_engine(engine):
    """Set the plotting engine to use.

    Parameters
    ----------
    engine : str
        Options are: 'matplotlib', 'plotly'.
    """

    if ((_p.mpl_available) or (_p.plotly_available)):
        try:
            assert(engine in _p.engines)
            _p.engine = engine
        except:
            error_message = "Tried to set an unknown engine \'{}\'.".format(
                                                                       engine)
            error_message += " Supported engines are {}".format(
                                                       [e for e in _p.engines])
            raise RuntimeError(error_message)
    else:
        warnings.warn("Tried to set \'{}\' but is not "
                      "available.".format(engine), RuntimeWarning)

    if ((_p.engine == "plotly") and
        (not _p.init_notebook_mode_set)):
        if (_p.is_ipython_kernel):
            _p.init_notebook_mode_set = True
            _p.plotly_module.init_notebook_mode(connected=True)


def get_engine():
    return _p.engine


def _check_incompatible_args_plotly(dpi, fig_size, ax):
    assert(_p.engine == "plotly")
    if(dpi or fig_size or ax):
        raise RuntimeError(
            "Plotly engine does not support setting 'dpi', 'fig_size' "
            "or 'ax', either leave these parameters unspecified, or "
            "select the matplotlib engine with"
            "'kwant.plotter.set_engine(\"matplotlib\")'")


def _sample_array(array, n_samples, rng=None):
    rng = _common.ensure_rng(rng)
    la = len(array)
    return array[rng.choice(range(la), min(n_samples, la), replace=False)]


# matplotlib helper functions.

def _color_cycle():
    """Infinitely cycle through colors from the matplotlib color cycle."""
    props = _p.matplotlib.rcParams['axes.prop_cycle']
    return itertools.cycle(x['color'] for x in props)


def _make_figure(dpi, fig_size, use_pyplot=False):
    if use_pyplot:
        # We import backends and pyplot only at the last possible moment (=now)
        from matplotlib import pyplot
        fig = pyplot.figure()
    else:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        fig = _p.Figure()
        fig.canvas = FigureCanvasAgg(fig)
    if dpi is not None:
        fig.set_dpi(dpi)
    if fig_size is not None:
        fig.set_figwidth(fig_size[0])
        fig.set_figheight(fig_size[1])
    return fig


def _maybe_output_fig(fig, file=None, show=True):
    """Output a matplotlib figure using a given output mode.

    Parameters
    ----------
    fig : matplotlib.figure.Figure instance
        The figure to be output.
    file : string or a file object
        The name of the target file or the target file itself
        (opened for writing).
    show : bool
        Whether to call ``matplotlib.pyplot.show()``.  Only has an effect if
        not saving to a file.

    Notes
    -----
    The behavior of this function producing a file is different from that of
    matplotlib in that the `dpi` attribute of the figure is used by defaul
    instead of the matplotlib config setting.
    """
    if fig is None:
        return

    if _p.engine == "matplotlib":
        if file is not None:
            fig.canvas.print_figure(file, dpi=fig.dpi)
        elif show:
            # If there was no file provided, pyplot should already be available
            # and we can import it safely without additional warnings.
            from matplotlib import pyplot
            pyplot.show()
    elif _p.engine == "plotly":
        if file is not None:
            _p.plotly_module.plot(fig, show_link=False, filename=file, auto_open=False)
        if show:
            if (_p.is_ipython_kernel):
                _p.plotly_module.iplot(fig)
            else:
                raise RuntimeError('show flag using the plotly engine can '
                                   'only be True if and only if called from a '
                                   'jupyter/ipython environment.')


def set_colors(color, collection, cmap, norm=None):
    """Process a color specification to a format accepted by collections.

    Parameters
    ----------
    color : color specification
    collection : instance of a subclass of ``matplotlib.collections.Collection``
        Collection to which the color is added.
    cmap : ``matplotlib`` color map specification or None
        Color map to be used if colors are specified as floats.
    norm : ``matplotlib`` color norm
        Norm to be used if colors are specified as floats.
    """

    length = max(len(collection.get_paths()), len(collection.get_offsets()))

    # matplotlib gets confused if dtype='object'
    if (isinstance(color, np.ndarray) and color.dtype == np.dtype('object')):
        color = tuple(color)

    if _p.has3d and isinstance(collection, _p.mplot3d.art3d.Line3DCollection):
        length = len(collection._segments3d)  # Once again, matplotlib fault!

    if _p.isarray(color) and len(color) == length:
        try:
            # check if it is an array of floats for color mapping
            color = np.asarray(color, dtype=float)
            if color.ndim == 1:
                collection.set_array(color)
                collection.set_cmap(cmap)
                collection.set_norm(norm)
                collection.set_color(None)
                return
        except (TypeError, ValueError):
            pass

    colors = _p.matplotlib.colors.colorConverter.to_rgba_array(color)
    collection.set_color(colors)


def percentile_bound(data, vmin, vmax, percentile=96, stretch=0.1):
    """Return the bounds that captures at least 'percentile' of 'data'.

    If 'vmin' or 'vmax' are provided, then the corresponding bound is
    exactly 'vmin' or 'vmax'. First we set the bounds such that the
    provided percentile of the data is within them. Then we try to
    extend the bounds to cover all the data, maximally stretching each
    bound by a factor 'stretch'.
    """
    if vmin is not None and vmax is not None:
        return vmin, vmax

    percentile = (100 - percentile) / 2
    percentiles = (0, percentile, 100 - percentile, 100)
    mn, bound_mn, bound_mx, mx = np.percentile(data.flatten(), percentiles)

    bound_mn = bound_mn if vmin is None else vmin
    bound_mx = bound_mx if vmax is None else vmax

    # Stretch the lower and upper bounds to cover all the data, if
    # we stretch the bound by less than a factor 'stretch'.
    stretch = (bound_mx - bound_mn) * stretch
    out_mn = max(bound_mn - stretch, mn) if vmin is None else vmin
    out_mx = min(bound_mx + stretch, mx) if vmax is None else vmax

    return (out_mn, out_mx)


symbol_dict = {'O': 'o', 's': ('p', 4, 45), 'S': ('P', 4, 45)}

def get_symbol(symbols):
    """Return the path corresponding to the description in ``symbols``"""
    # Figure out if list of symbols or single symbol.
    if not hasattr(symbols, '__getitem__'):
        symbols = [symbols]
    elif len(symbols) == 3 and symbols[0] in ('p', 'P'):
        # Most likely a polygon specification (at least not a valid other
        # symbol).
        symbols = [symbols]

    symbols = [symbol_dict[symbol] if symbol in symbol_dict else symbol for
               symbol in symbols]

    paths = []
    for symbol in symbols:
        if isinstance(symbol, _p.matplotlib.path.Path):
            return symbol
        elif hasattr(symbol, '__getitem__') and len(symbol) == 3:
            kind, n, angle = symbol

            if kind in ['p', 'P']:
                if kind == 'p':
                    radius = 1. / cos(pi / n)
                else:
                    # make the polygon such that it has area equal
                    # to a unit circle
                    radius = sqrt(2 * pi / (n * sin(2 * pi / n)))

                angle = pi * angle / 180
                patch = _p.matplotlib.patches.RegularPolygon((0, 0), n,
                                                             radius=radius,
                                                             orientation=angle)
            else:
                raise ValueError("Unknown symbol definition " + str(symbol))
        elif symbol == 'o':
            patch = _p.matplotlib.patches.Circle((0, 0), 1)

        paths.append(patch.get_path().transformed(patch.get_transform()))

    return paths


def symbols(axes, pos, symbol='o', size=1, reflen=None, facecolor='k',
            edgecolor='k', linewidth=None, cmap=None, norm=None, zorder=0,
            **kwargs):
    """Add a collection of symbols (2D or 3D) to an axes instance.

    Parameters
    ----------
    axes : matplotlib.axes.Axes instance
        Axes to which the lines have to be added.
    pos0 : 2d or 3d array_like
        Coordinates of each symbol.
    symbol: symbol definition.
        TODO To be written.
    size: float or 1d array
        Size(s) of the symbols. Defaults to 1.
    reflen: float or None, optional
        If ``reflen`` is ``None``, the symbol sizes and linewidths are
        given in points (absolute size in the figure space). If
        ``reflen`` is a number, the symbol sizes and linewidths are
        given in units of ``reflen`` in data space (i.e. scales with the
        scale of the plot). Defaults to ``None``.
    facecolor: color definition, optional
    edgecolor: color definition, optional
        Defines the fill and edge color of the symbol, repsectively.
        Either a single object that is a proper matplotlib color
        definition or a sequence of such objects of appropriate
        length.  Defaults to all black.
    cmap : ``matplotlib`` color map specification or None
        Color map to be used if colors are specified as floats.
    norm : ``matplotlib`` color norm
        Norm to be used if colors are specified as floats.
    zorder: int
        Order in which different collections are drawn: larger
        ``zorder`` means the collection is drawn over collections with
        smaller ``zorder`` values.
    **kwargs : dict keyword arguments to
        pass to `PathCollection` or `Path3DCollection`, respectively.

    Returns
    -------
    `PathCollection` or `Path3DCollection` instance containing all the
    symbols that were added.
    """

    dim = pos.shape[1]
    assert dim == 2 or dim == 3

    #internally, size must be array_like
    try:
        size[0]
    except TypeError:
        size = (size, )

    if dim == 2:
        Collection = _p.PathCollection
    else:
        Collection = _p.Path3DCollection

    if len(pos) == 0 or np.all(symbol == 'no symbol') or np.all(size == 0):
        paths = []
        pos = np.empty((0, dim))
    else:
        paths = get_symbol(symbol)

    coll = Collection(paths, sizes=size, reflen=reflen, linewidths=linewidth,
                      offsets=pos, transOffset=axes.transData, zorder=zorder)

    set_colors(facecolor, coll, cmap, norm)
    coll.set_edgecolors(edgecolor)

    coll.update(kwargs)

    if dim == 2:
        axes.add_collection(coll)
    else:
        axes.add_collection3d(coll)

    return coll


def lines(axes, pos0, pos1, reflen=None, colors='k', linestyles='solid',
          cmap=None, norm=None, zorder=0, **kwargs):
    """Add a collection of line segments (2D or 3D) to an axes instance.

    Parameters
    ----------
    axes : matplotlib.axes.Axes instance
        Axes to which the lines have to be added.
    pos0 : 2d or 3d array_like
        Starting coordinates of each line segment
    pos1 : 2d or 3d array_like
        Ending coordinates of each line segment
    reflen: float or None, optional
        If `reflen` is `None`, the linewidths are given in points (absolute
        size in the figure space). If `reflen` is a number, the linewidths
        are given in units of `reflen` in data space (i.e. scales with
        the scale of the plot). Defaults to `None`.
    colors : color definition, optional
        Either a single object that is a proper matplotlib color definition
        or a sequence of such objects of appropriate length.  Defaults to all
        segments black.
    linestyles :linestyle definition, optional
        Either a single object that is a proper matplotlib line style
        definition or a sequence of such objects of appropriate length.
        Defaults to all segments solid.
    cmap : ``matplotlib`` color map specification or None
        Color map to be used if colors are specified as floats.
    norm : ``matplotlib`` color norm
        Norm to be used if colors are specified as floats.
    zorder: int
        Order in which different collections are drawn: larger
        `zorder` means the collection is drawn over collections with
        smaller `zorder` values.
    **kwargs : dict keyword arguments to
        pass to `LineCollection` or `Line3DCollection`, respectively.

    Returns
    -------
    `LineCollection` or `Line3DCollection` instance containing all the
    segments that were added.
    """

    if not pos0.shape == pos1.shape:
        raise ValueError('Incompatible lengths of coordinate arrays.')

    dim = pos0.shape[1]
    assert dim == 2 or dim == 3
    if dim == 2:
        Collection = _p.LineCollection
    else:
        Collection = _p.Line3DCollection

    if (len(pos0) == 0 or
        ('linewidths' in kwargs and kwargs['linewidths'] == 0)):
        coll = Collection([], reflen=reflen, linestyles=linestyles,
                          zorder=zorder)
        coll.update(kwargs)
        if dim == 2:
            axes.add_collection(coll)
        else:
            axes.add_collection3d(coll)
        return coll

    segments = np.c_[pos0, pos1].reshape(pos0.shape[0], 2, dim)

    coll = Collection(segments, reflen=reflen, linestyles=linestyles,
                      zorder=zorder)
    set_colors(colors, coll, cmap, norm)
    coll.update(kwargs)

    if dim == 2:
        axes.add_collection(coll)
    else:
        axes.add_collection3d(coll)

    return coll


# Extracting necessary data from the system.

def sys_leads_sites(sys, num_lead_cells=2):
    """Return all the sites of the system and of the leads as a list.

    Parameters
    ----------
    sys : kwant.builder.Builder or kwant.system.System instance
        The system, sites of which should be returned.
    num_lead_cells : integer
        The number of times lead sites from each lead should be returned.
        This is useful for showing several unit cells of the lead next to the
        system.

    Returns
    -------
    sites : list of (site, lead_number, copy_number) tuples
        A site is a `~kwant.builder.Site` instance if the system is not finalized,
        and an integer otherwise.  For system sites `lead_number` is `None` and
        `copy_number` is `0`, for leads both are integers.
    lead_cells : list of slices
        `lead_cells[i]` gives the position of all the coordinates of lead
        `i` within `sites`.

    Notes
    -----
    Leads are only supported if they are of the same type as the original
    system, i.e.  sites of `~kwant.builder.BuilderLead` leads are returned with an
    unfinalized system, and sites of ``system.InfiniteSystem`` leads are
    returned with a finalized system.
    """
    syst = sys  # for naming consistency within function bodies
    lead_cells = []
    if isinstance(syst, builder.Builder):
        sites = [(site, None, 0) for site in syst.sites()]
        for leadnr, lead in enumerate(syst.leads):
            start = len(sites)
            if hasattr(lead, 'builder') and len(lead.interface):
                sites.extend(((site, leadnr, i) for site in
                              lead.builder.sites() for i in
                              range(num_lead_cells)))
            lead_cells.append(slice(start, len(sites)))
    elif isinstance(syst, system.FiniteSystem):
        sites = [(i, None, 0) for i in range(syst.graph.num_nodes)]
        for leadnr, lead in enumerate(syst.leads):
            start = len(sites)
            # We will only plot leads with a graph and with a symmetry.
            if (hasattr(lead, 'graph') and hasattr(lead, 'symmetry') and
                len(syst.lead_interfaces[leadnr])):
                sites.extend(((site, leadnr, i) for site in
                              range(lead.cell_size) for i in
                              range(num_lead_cells)))
            lead_cells.append(slice(start, len(sites)))
    else:
        raise TypeError('Unrecognized system type.')
    return sites, lead_cells


def sys_leads_pos(sys, site_lead_nr):
    """Return an array of positions of sites in a system.

    Parameters
    ----------
    sys : `kwant.builder.Builder` or `kwant.system.System` instance
        The system, coordinates of sites of which should be returned.
    site_lead_nr : list of `(site, leadnr, copynr)` tuples
        Output of `sys_leads_sites` applied to the system.

    Returns
    -------
    coords : numpy.ndarray of floats
        Array of coordinates of the sites.

    Notes
    -----
    This function uses `site.pos` property to get the position of a builder
    site and `sys.pos(sitenr)` for finalized systems.  This function requires
    that all the positions of all the sites have the same dimensionality.
    """

    # Note about efficiency (also applies to sys_leads_hoppings_pos)
    # NumPy is really slow when making a NumPy array from a tinyarray
    # (buffer interface seems very slow). It's much faster to first
    # convert to a tuple and then to convert to numpy array ...

    syst = sys  # for naming consistency inside function bodies
    is_builder = isinstance(syst, builder.Builder)
    num_lead_cells = site_lead_nr[-1][2] + 1
    if is_builder:
        pos = np.array(ta.array([i[0].pos for i in site_lead_nr]))
    else:
        syst_from_lead = lambda lead: (syst if (lead is None)
                                      else syst.leads[lead])
        pos = np.array(ta.array([syst_from_lead(i[1]).pos(i[0])
                                 for i in site_lead_nr]))
    if pos.dtype == object:  # Happens if not all the pos are same length.
        raise ValueError("pos attribute of the sites does not have consistent"
                         " values.")
    dim = pos.shape[1]

    def get_vec_domain(lead_nr):
        if lead_nr is None:
            return np.zeros((dim,)), 0
        if is_builder:
            sym = syst.leads[lead_nr].builder.symmetry
            try:
                site = syst.leads[lead_nr].interface[0]
            except IndexError:
                return (0, 0)
        else:
            try:
                sym = syst.leads[lead_nr].symmetry
                site = syst.sites[syst.lead_interfaces[lead_nr][0]]
            except (AttributeError, IndexError):
                # empty leads, or leads without symmetry aren't drawn anyways
                return (0, 0)
        dom = sym.which(site)[0] + 1
        # Conversion to numpy array here useful for efficiency
        vec = np.array(sym.periods)[0]
        return vec, dom
    vecs_doms = dict((i, get_vec_domain(i)) for i in range(len(syst.leads)))
    vecs_doms[None] = np.zeros((dim,)), 0
    for k, v in vecs_doms.items():
        vecs_doms[k] = [v[0] * i for i in range(v[1], v[1] + num_lead_cells)]
    pos += [vecs_doms[i[1]][i[2]] for i in site_lead_nr]
    return pos


def sys_leads_hoppings(sys, num_lead_cells=2):
    """Return all the hoppings of the system and of the leads as an iterator.

    Parameters
    ----------
    sys : kwant.builder.Builder or kwant.system.System instance
        The system, sites of which should be returned.
    num_lead_cells : integer
        The number of times lead sites from each lead should be returned.
        This is useful for showing several unit cells of the lead next to the
        system.

    Returns
    -------
    hoppings : list of (hopping, lead_number, copy_number) tuples
        A site is a `~kwant.builder.Site` instance if the system is not finalized,
        and an integer otherwise.  For system sites `lead_number` is `None` and
        `copy_number` is `0`, for leads both are integers.
    lead_cells : list of slices
        `lead_cells[i]` gives the position of all the coordinates of lead
        `i` within `hoppings`.

    Notes
    -----
    Leads are only supported if they are of the same type as the original
    system, i.e.  hoppings of `~kwant.builder.BuilderLead` leads are returned with an
    unfinalized system, and hoppings of `~kwant.system.InfiniteSystem` leads are
    returned with a finalized system.
    """

    syst = sys  # for naming consistency inside function bodies
    hoppings = []
    lead_cells = []
    if isinstance(syst, builder.Builder):
        hoppings.extend(((hop, None, 0) for hop in syst.hoppings()))

        def lead_hoppings(lead):
            sym = lead.symmetry
            for site2, site1 in lead.hoppings():
                shift1 = sym.which(site1)[0]
                shift2 = sym.which(site2)[0]
                # We need to make sure that the hopping is between a site in a
                # fundamental domain and a site with a negative domain.  The
                # direction of the hopping is chosen arbitrarily
                # NOTE(Anton): This may need to be revisited with the future
                # builder format changes.
                shift = max(shift1, shift2)
                yield sym.act([-shift], site2), sym.act([-shift], site1)

        for leadnr, lead in enumerate(syst.leads):
            start = len(hoppings)
            if hasattr(lead, 'builder') and len(lead.interface):
                hoppings.extend(((hop, leadnr, i) for hop in
                                 lead_hoppings(lead.builder) for i in
                                 range(num_lead_cells)))
            lead_cells.append(slice(start, len(hoppings)))
    elif isinstance(syst, system.System):
        def ll_hoppings(syst):
            for i in range(syst.graph.num_nodes):
                for j in syst.graph.out_neighbors(i):
                    if i < j:
                        yield i, j

        hoppings.extend(((hop, None, 0) for hop in ll_hoppings(syst)))
        for leadnr, lead in enumerate(syst.leads):
            start = len(hoppings)
            # We will only plot leads with a graph and with a symmetry.
            if (hasattr(lead, 'graph') and hasattr(lead, 'symmetry') and
                len(syst.lead_interfaces[leadnr])):
                hoppings.extend(((hop, leadnr, i) for hop in ll_hoppings(lead)
                                 for i in range(num_lead_cells)))
            lead_cells.append(slice(start, len(hoppings)))
    else:
        raise TypeError('Unrecognized system type.')
    return hoppings, lead_cells


def sys_leads_hopping_pos(sys, hop_lead_nr):
    """Return arrays of coordinates of all hoppings in a system.

    Parameters
    ----------
    sys : ``~kwant.builder.Builder`` or ``~kwant.system.System`` instance
        The system, coordinates of sites of which should be returned.
    hoppings : list of ``(hopping, leadnr, copynr)`` tuples
        Output of `sys_leads_hoppings` applied to the system.

    Returns
    -------
    coords : (end_site, start_site): tuple of NumPy arrays of floats
        Array of coordinates of the hoppings.  The first half of coordinates
        in each array entry are those of the first site in the hopping, the
        last half are those of the second site.

    Notes
    -----
    This function uses ``site.pos`` property to get the position of a builder
    site and ``sys.pos(sitenr)`` for finalized systems.  This function requires
    that all the positions of all the sites have the same dimensionality.
    """

    syst = sys  # for naming consistency inside function bodies
    is_builder = isinstance(syst, builder.Builder)
    if len(hop_lead_nr) == 0:
        return np.empty((0, 3)), np.empty((0, 3))
    num_lead_cells = hop_lead_nr[-1][2] + 1
    if is_builder:
        pos = np.array(ta.array([ta.array(tuple(i[0][0].pos) +
                                          tuple(i[0][1].pos)) for i in
                                 hop_lead_nr]))
    else:
        syst_from_lead = lambda lead: (syst if (lead is None) else
                                      syst.leads[lead])
        pos = ta.array([ta.array(tuple(syst_from_lead(i[1]).pos(i[0][0])) +
                                 tuple(syst_from_lead(i[1]).pos(i[0][1]))) for i
                        in hop_lead_nr])
        pos = np.array(pos)
    if pos.dtype == object:  # Happens if not all the pos are same length.
        raise ValueError("pos attribute of the sites does not have consistent"
                         " values.")
    dim = pos.shape[1]

    def get_vec_domain(lead_nr):
        if lead_nr is None:
            return np.zeros((dim,)), 0
        if is_builder:
            sym = syst.leads[lead_nr].builder.symmetry
            try:
                site = syst.leads[lead_nr].interface[0]
            except IndexError:
                return (0, 0)
        else:
            try:
                sym = syst.leads[lead_nr].symmetry
                site = syst.sites[syst.lead_interfaces[lead_nr][0]]
            except (AttributeError, IndexError):
                # empyt leads or leads without symmetry are not drawn anyways
                return (0, 0)
        dom = sym.which(site)[0] + 1
        vec = np.array(sym.periods)[0]
        return np.r_[vec, vec], dom

    vecs_doms = dict((i, get_vec_domain(i)) for i in range(len(syst.leads)))
    vecs_doms[None] = np.zeros((dim,)), 0
    for k, v in vecs_doms.items():
        vecs_doms[k] = [v[0] * i for i in range(v[1], v[1] + num_lead_cells)]
    pos += [vecs_doms[i[1]][i[2]] for i in hop_lead_nr]
    return np.copy(pos[:, : dim // 2]), np.copy(pos[:, dim // 2:])


# Useful plot functions (to be extended).
# The default plotly symbol size is a 6 px
# The keys of 2, and 3 represent the dimension of the system.
# e.g. the default for site_size for kwant system of dim=2 is 0.25, and
# dim=3 is 0.5
defaults = {'site_symbol': {2: 'o', 3: 'o'},
            'site_size': {2: 0.25, 3: 0.5},
            'plotly_site_size_reference': 6,
            'site_color': {2: 'black', 3: 'white'},
            'site_edgecolor': {2: 'black', 3: 'black'},
            'site_lw': {2: 0, 3: 0.1},
            'hop_color': {2: 'black', 3: 'black'},
            'hop_lw': {2: 0.1, 3: 0},
            'lead_color': {2: 'red', 3: 'red'}}


def plot(sys, num_lead_cells=2, unit=None,
         site_symbol=None, site_size=None,
         site_color=None, site_edgecolor=None, site_lw=None,
         hop_color=None, hop_lw=None,
         lead_site_symbol=None, lead_site_size=None, lead_color=None,
         lead_site_edgecolor=None, lead_site_lw=None,
         lead_hop_lw=None, pos_transform=None,
         cmap='gray', colorbar=True, file=None,
         show=True, dpi=None, fig_size=None, ax=None):
    """Plot a system in 2 or 3 dimensions.

    An alias exists for this common name: ``kwant.plot``.

    Parameters
    ----------
    sys : kwant.builder.Builder or kwant.system.FiniteSystem
        A system to be plotted.
    num_lead_cells : int
        Number of lead copies to be shown with the system.
    unit : 'nn', 'pt', or float
        The unit used to specify symbol sizes and linewidths.
        Possible choices are:

        - 'nn': unit is the shortest hopping or a typical nearst neighbor
          distance in the system if there are no hoppings.  This means that
          symbol sizes/linewidths will scale as the zoom level of the figure is
          changed.  Very short distances are discarded before searching for the
          shortest.  This choice means that the symbols will scale if the
          figure is zoomed.
        - 'pt': unit is points (point = 1/72 inch) in figure space.  This means
          that symbols and linewidths will always be drawn with the same size
          independent of zoom level of the plot.
        - float: sizes are given in units of this value in real (system) space,
          and will accordingly scale as the plot is zoomed.

        The default value is 'nn', which allows to ensure that the images
        neighboring sites do not overlap.

    site_symbol : symbol specification, function, array, or `None`
        Symbol used for representing a site in the plot. Can be specified as

        - 'o': circle with radius of 1 unit.
        - 's': square with inner circle radius of 1 unit.
        - ``('p', nvert, angle)``: regular polygon with ``nvert`` vertices,
          rotated by ``angle``. ``angle`` is given in degrees, and ``angle=0``
          corresponds to one edge of the polygon pointing upward. The
          radius of the inner circle is 1 unit. [Unsupported by plotly engine]
        - 'no symbol': no symbol is plotted. [Unsupported by plotly engine]
        - 'S', `('P', nvert, angle)`: as the lower-case variants described
          above, but with an area equal to a circle of radius 1. (Makes
          the visual size of the symbol equal to the size of a circle with
          radius 1). [Unsupported by plotly engine]
        - matplotlib.path.Path instance. [Unsupported by plotly engine]

        Instead of a single symbol, different symbols can be specified
        for different sites by passing a function that returns a valid
        symbol specification for each site, or by passing an array of
        symbols specifications (only for kwant.system.FiniteSystem).
    site_size : number, function, array, or `None`
        Relative (linear) size of the site symbol.
        An array may not be used when 'syst' is a kwant.Builder.
    site_color : ``matplotlib`` color description, function, array, or `None`
        A color used for plotting a site in the system. If a colormap is used,
        it should be a function returning single floats or a one-dimensional
        array of floats. By default sites are colored by their site family,
        using the current matplotlib color cycle.
        An array of colors may not be used when 'syst' is a kwant.Builder.
    site_edgecolor : ``matplotlib`` color description, function, array, or `None`
        Color used for plotting the edges of the site symbols. Only
        valid matplotlib color descriptions are allowed (and no
        combination of floats and colormap as for site_color).
        An array of colors may not be used when 'syst' is a kwant.Builder.
    site_lw : number, function, array, or `None`
        Linewidth of the site symbol edges.
        An array may not be used when 'syst' is a kwant.Builder.
    hop_color : ``matplotlib`` color description or a function
        Same as `site_color`, but for hoppings.  A function is passed two sites
        in this case. (arrays are not allowed in this case).
    hop_lw : number, function, or `None`
        Linewidth of the hoppings.
    lead_site_symbol : symbol specification or `None`
        Symbol to be used for the leads. See `site_symbol` for allowed
        specifications. Note that for leads, only constants
        (i.e. no functions or arrays) are allowed. If None, then
        `site_symbol` is used if it is constant (i.e. no function or array),
        the default otherwise. The same holds for the other lead properties
        below.
    lead_site_size : number or `None`
        Relative (linear) size of the lead symbol
    lead_color : ``matplotlib`` color description or `None`
        For the leads, `num_lead_cells` copies of the lead unit cell
        are plotted. They are plotted in color fading from `lead_color`
        to white (alpha values in `lead_color` are supported) when moving
        from the system into the lead. Is also applied to the
        hoppings.
    lead_site_edgecolor : ``matplotlib`` color description or `None`
        Color of the symbol edges (no fading done).
    lead_site_lw : number or `None`
        Linewidth of the lead symbols.
    lead_hop_lw : number or `None`
        Linewidth of the lead hoppings.
    cmap : ``matplotlib`` color map or a sequence of two color maps or `None`
        The color map used for sites and optionally hoppings.
    pos_transform : function or `None`
        Transformation to be applied to the site position.
    colorbar : bool
        Whether to show a colorbar if colormap is used. Ignored if `ax` is
        provided.
    file : string or file object or `None`
        The output file.  If `None`, output will be shown instead.
    show : bool
        Whether ``matplotlib.pyplot.show()`` is to be called, and the output is
        to be shown immediately.  Defaults to `True`.
    dpi : float or `None`
        Number of pixels per inch.  If not set the ``matplotlib`` default is
        used.
    fig_size : tuple or `None`
        Figure size `(width, height)` in inches.  If not set, the default
        ``matplotlib`` value is used.
    ax : ``matplotlib.axes.Axes`` instance or `None`
        If `ax` is not `None`, no new figure is created, but the plot is done
        within the existing Axes `ax`. in this case, `file`, `show`, `dpi`
        and `fig_size` are ignored.

    Returns
    -------
    fig : matplotlib figure
        A figure with the output if `ax` is not set, else None.

    Notes
    -----
    - If `None` is passed for a plot property, a default value depending on
      the dimension is chosen. Typically, the default values result in
      acceptable plots.

    - The meaning of "site" depends on whether the system to be plotted is a
      builder or a low level system.  For builders, a site is a
      kwant.builder.Site object.  For low level systems, a site is an integer
      -- the site number.

    - color and symbol definitions may be tuples, but not lists or arrays.
      Arrays of values (linewidths, colors, sizes) may not be tuples.

    - The dimensionality of the plot (2D vs 3D) is inferred from the coordinate
      array.  If there are more than three coordinates, only the first three
      are used.  If there is just one coordinate, the second one is padded with
      zeros.

    - The system is scaled to fit the smaller dimension of the figure, given
      its aspect ratio.

    """

    # Provide default unit if user did not specify
    if _p.engine == "matplotlib":
        fig = _plot_matplotlib(sys, num_lead_cells, unit,
                        site_symbol, site_size,
                        site_color, site_edgecolor, site_lw,
                        hop_color, hop_lw,
                        lead_site_symbol, lead_site_size, lead_color,
                        lead_site_edgecolor, lead_site_lw,
                        lead_hop_lw, pos_transform,
                        cmap, colorbar, file,
                        show, dpi, fig_size, ax)
    elif _p.engine == "plotly":
        _check_incompatible_args_plotly(dpi, fig_size, ax)
        fig = _plot_plotly(sys, num_lead_cells, unit,
                        site_symbol, site_size,
                        site_color, site_edgecolor, site_lw,
                        hop_color, hop_lw,
                        lead_site_symbol, lead_site_size, lead_color,
                        lead_site_edgecolor, lead_site_lw,
                        lead_hop_lw, pos_transform,
                        cmap, colorbar, file,
                        show)
    elif _p.engine is None:
        raise RuntimeError("Cannot use plot() without a plotting lib installed")
    else:
        raise RuntimeError("plot() does not support engine '{}'".format(_p.engine))

    _maybe_output_fig(fig, file=file, show=show)

    return fig

def _resize_to_dim(array, dim):
    if array.shape[1] != dim:
        ar = np.zeros((len(array), dim), dtype=float)
        ar[:, : min(dim, array.shape[1])] = array[
            :, : min(dim, array.shape[1])]
        return ar
    else:
        return array


def _check_length(name, loc):
    value = loc[name]
    if name in ('site_size', 'site_lw') and isinstance(value, tuple):
        raise TypeError('{0} may not be a tuple, use list or '
                        'array instead.'.format(name))
    if isinstance(value, (str, tuple)):
        return
    try:
        if len(value) != loc['n_syst_sites']:
            raise ValueError('Length of {0} is not equal to number of '
                             'system sites.'.format(name))
    except TypeError:
        pass

# make all specs proper: either constant or lists/np.arrays:
def _make_proper_site_spec(spec_name,  spec, syst, sites, fancy_indexing=False):
    if _p.isarray(spec) and isinstance(syst, builder.Builder):
        raise TypeError('{} cannot be an array when plotting'
                        ' a Builder; use a function instead.'
                        .format(spec_name))
    if callable(spec):
        spec = [spec(i[0]) for i in sites if i[1] is None]
    if (fancy_indexing and _p.isarray(spec)
        and not isinstance(spec, np.ndarray)):
        try:
            spec = np.asarray(spec)
        except:
            spec = np.asarray(spec, dtype='object')
    return spec

def _make_proper_hop_spec(spec, hops, fancy_indexing=False):
    if callable(spec):
        spec = [spec(*i[0]) for i in hops if i[1] is None]
    if (fancy_indexing and _p.isarray(spec)
        and not isinstance(spec, np.ndarray)):
        try:
            spec = np.asarray(spec)
        except:
            spec = np.asarray(spec, dtype='object')
    return spec

def _plot_plotly(sys, num_lead_cells, unit,
         site_symbol, site_size,
         site_color, site_edgecolor, site_lw,
         hop_color, hop_lw,
         lead_site_symbol, lead_site_size, lead_color,
         lead_site_edgecolor, lead_site_lw,
         lead_hop_lw, pos_transform,
         cmap, colorbar, file,
         show, fig=None):

    if unit is None:
        unit = 'pt'

    syst = sys  # for naming consistency inside function bodies
    # Generate data.
    sites, lead_sites_slcs = sys_leads_sites(syst, num_lead_cells)
    n_syst_sites = sum(i[1] is None for i in sites)
    sites_pos = sys_leads_pos(syst, sites)
    hops, lead_hops_slcs = sys_leads_hoppings(syst, num_lead_cells)
    n_syst_hops = sum(i[1] is None for i in hops)
    end_pos, start_pos = sys_leads_hopping_pos(syst, hops)

    loc = locals()

    for name in ['site_symbol', 'site_size', 'site_color', 'site_edgecolor',
                 'site_lw']:
        _check_length(name, loc)

    # Apply transformations to the data
    if pos_transform is not None:
        sites_pos = np.apply_along_axis(pos_transform, 1, sites_pos)
        end_pos = np.apply_along_axis(pos_transform, 1, end_pos)
        start_pos = np.apply_along_axis(pos_transform, 1, start_pos)

    dim = 3 if (sites_pos.shape[1] == 3) else 2

    sites_pos = _resize_to_dim(sites_pos, dim)
    end_pos = _resize_to_dim(end_pos, dim)
    start_pos = _resize_to_dim(start_pos, dim)

    # Determine the reference length.
    if unit != 'pt':
        raise RuntimeError('Plotly engine currently only supports '
                         'the pt symbol size unit')

    site_symbol = _make_proper_site_spec('site_symbol', site_symbol, syst, sites)
    if site_symbol is None: site_symbol = defaults['site_symbol'][dim]
    # separate different symbols (not done in 3D, the separation
    # would mess up sorting)
    if (_p.isarray(site_symbol) and dim != 3 and
        (len(site_symbol) != 3 or site_symbol[0] not in ('p', 'P'))):
        symbol_dict = defaultdict(list)
        for i, symbol in enumerate(site_symbol):
            symbol_dict[symbol].append(i)
        symbol_slcs = []
        for symbol, indx in symbol_dict.items():
            symbol_slcs.append((symbol, np.array(indx)))
        fancy_indexing = True
    else:
        symbol_slcs = [(site_symbol, slice(n_syst_sites))]
        fancy_indexing = False

    if site_color is None:
        cycle = _color_cycle()
        if isinstance(syst, (builder.FiniteSystem, builder.InfiniteSystem)):
            # Skipping the leads for brevity.
            families = sorted({site.family for site in syst.sites})
            color_mapping = dict(zip(families, cycle))
            def site_color(site):
                return color_mapping[syst.sites[site].family]
        elif isinstance(syst, builder.Builder):
            families = sorted({site[0].family for site in sites})
            color_mapping = dict(zip(families, cycle))
            def site_color(site):
                return color_mapping[site.family]
        else:
            # Unknown finalized system, no sites access.
            site_color = defaults['site_color'][dim]

    site_size = _make_proper_site_spec('site_size',site_size, syst, sites, fancy_indexing)
    site_color = _make_proper_site_spec('site_color',site_color, syst, sites, fancy_indexing)
    site_edgecolor = _make_proper_site_spec('site_edgecolor',site_edgecolor, syst, sites,
                                            fancy_indexing)
    site_lw = _make_proper_site_spec('site_lw',site_lw, syst, sites, fancy_indexing)

    hop_color = _make_proper_hop_spec(hop_color, hops)
    hop_lw = _make_proper_hop_spec(hop_lw, hops)

    # Choose defaults depending on dimension, if None was given
    if site_size is None: site_size = defaults['site_size'][dim]
    if site_edgecolor is None:
        site_edgecolor = defaults['site_edgecolor'][dim]
    if site_lw is None: site_lw = defaults['site_lw'][dim]

    if hop_color is None: hop_color = defaults['hop_color'][dim]
    if hop_lw is None: hop_lw = defaults['hop_lw'][dim]

    if len(symbol_slcs) > 1:
        try:
            if site_color.ndim == 1 and len(site_color) == n_syst_sites:
                site_color = np.asarray(site_color, dtype=float)
        except:
            pass

    # take spec also for lead, if it's not a list/array, default, otherwise
    if lead_site_symbol is None:
        lead_site_symbol = (site_symbol if not _p.isarray(site_symbol)
                            else defaults['site_symbol'][dim])
    if lead_site_size is None:
        lead_site_size = (site_size if not _p.isarray(site_size)
                          else defaults['site_size'][dim])
    if lead_color is None:
        lead_color = defaults['lead_color'][dim]
    lead_color = _p.matplotlib.colors.colorConverter.to_rgba(lead_color)

    if lead_site_edgecolor is None:
        lead_site_edgecolor = (site_edgecolor if not _p.isarray(site_edgecolor)
                               else defaults['site_edgecolor'][dim])
    if lead_site_lw is None:
        lead_site_lw = (site_lw if not _p.isarray(site_lw)
                        else defaults['site_lw'][dim])
    if lead_hop_lw is None:
        lead_hop_lw = (hop_lw if not _p.isarray(hop_lw)
                       else defaults['hop_lw'][dim])

    hop_cmap = None
    if not isinstance(cmap, str):
        try:
            cmap, hop_cmap = cmap
        except TypeError:
            pass
    # Plot system sites and hoppings

    # First plot the nodes (sites) of the graph
    assert dim == 2 or dim == 3
    site_node_trace, site_edge_trace = [], []
    for symbol, slc in symbol_slcs:
        site_symbol_plotly = _p.convert_symbol_mpl_plotly(symbol)
        if site_symbol_plotly == -1:
            # The kwant documentation supports no symbol as a string argument for site_symbol
            # If it evaluates to -1, then the user has specified "no symbol" as the input.
            # https://kwant-project.org/doc/1/reference/generated/kwant.plotter.plot
            continue
        size = site_size[slc] if _p.isarray(site_size) else site_size
        col = site_color[slc] if _p.isarray(site_color) else site_color
        if _p.isarray(site_edgecolor) or _p.isarray(site_lw):
            raise RuntimeError("Plotly engine not currently support an array "
                               "of linecolors or linewidths. Please restrict "
                               "to only a constant (i.e. no function or array)"
                               " site_edgecolor and site_lw property "
                               "for the entire plot.")
        else:
            edgecol = site_edgecolor if not isinstance(site_edgecolor, tuple) \
                           else _p.convert_colormap_mpl_plotly(*site_edgecolor)
            lw = site_lw

        if dim == 3:
            x, y, z = sites_pos[slc].transpose()
            site_node_trace_elem = _p.plotly_graph_objs.Scatter3d(x=x, y=y,
                                                                  z=z)
            site_node_trace_elem.marker.symbol = _p.convert_symbol_mpl_plotly_3d(
                                                                        symbol)
        else:
            x, y = sites_pos[slc].transpose()
            site_node_trace_elem = _p.plotly_graph_objs.Scatter(x=x, y=y)
            site_node_trace_elem.marker.symbol = _p.convert_symbol_mpl_plotly(
                                                                        symbol)

        site_node_trace_elem.mode = 'markers'
        site_node_trace_elem.hoverinfo = 'none'
        site_node_trace_elem.marker.showscale = False
        site_node_trace_elem.marker.colorscale = \
                                          _p.convert_cmap_list_mpl_plotly(cmap)
        site_node_trace_elem.marker.reversescale = False
        marker_color = col if not isinstance(col, tuple) \
                           else _p.convert_colormap_mpl_plotly(*col)
        site_node_trace_elem.marker.color = marker_color
        site_node_trace_elem.marker.size = \
                                _p.convert_site_size_mpl_plotly(size,
                                        defaults['plotly_site_size_reference'])

        site_node_trace_elem.line.width = lw
        site_node_trace_elem.line.color = edgecol
        site_node_trace_elem.showlegend = False

        site_node_trace.append(site_node_trace_elem)

    # Now plot the edges (hops) of the graph
    end, start = end_pos[: n_syst_hops], start_pos[: n_syst_hops]

    if dim == 3:
        x0, y0, z0 = end.transpose()
        x1, y1, z1 = start.transpose()
        nones = [None] * len(x0)
        site_edge_trace_elem = _p.plotly_graph_objs.Scatter3d(
                                    x=np.array([x0, x1, nones]).transpose().flatten(),
                                    y=np.array([y0, y1, nones]).transpose().flatten(),
                                    z=np.array([z0, z1, nones]).transpose().flatten()
                                )
    else:
        x0, y0 = end.transpose()
        x1, y1 = start.transpose()
        nones = [None] * len(x0)
        site_edge_trace_elem = _p.plotly_graph_objs.Scatter(
                                    x=np.array([x0, x1, nones]).transpose().flatten(),
                                    y=np.array([y0, y1, nones]).transpose().flatten()
                                )

    if _p.isarray(hop_color) or _p.isarray(hop_lw):
        raise RuntimeError("Plotly engine not currently support an array "
                               "of linecolors or linewidths. Please restrict "
                               "to only a constant (i.e. no function or array)"
                               " hop_color and hop_lw property "
                               "for the entire plot.")
    site_edge_trace_elem.line.width = hop_lw
    site_edge_trace_elem.line.color = hop_color
    site_edge_trace_elem.hoverinfo = 'none'
    site_edge_trace_elem.showlegend = False
    site_edge_trace_elem.mode = 'lines'
    site_edge_trace.append(site_edge_trace_elem)

    # Plot lead sites and edges

    lead_node_trace, lead_edge_trace = [], []
    for sites_slc, hops_slc in zip(lead_sites_slcs, lead_hops_slcs):
        lead_site_colors = np.array([i[2] for i in sites[sites_slc]],
                                    dtype=float)
        if dim == 3:

            x, y, z = sites_pos[sites_slc].transpose()
            lead_node_trace_elem = _p.plotly_graph_objs.Scatter3d(x=x, y=y,
                                                                  z=z)
            lead_node_trace_elem.marker.symbol = \
                              _p.convert_symbol_mpl_plotly_3d(lead_site_symbol)
        else:
            x, y = sites_pos[sites_slc].transpose()
            lead_node_trace_elem = _p.plotly_graph_objs.Scatter(x=x, y=y)
            lead_site_symbol_plotly = _p.convert_symbol_mpl_plotly(lead_site_symbol)
            if lead_site_symbol_plotly == -1:
                # The kwant documentation supports no symbol as a string argument for site_symbol
                # If it evaluates to -1, then the user has specified "no symbol" as the input.
                # https://kwant-project.org/doc/1/reference/generated/kwant.plotter.plot
                continue
            lead_node_trace_elem.marker.symbol = lead_site_symbol_plotly

        lead_node_trace_elem.mode = 'markers'
        lead_node_trace_elem.hoverinfo = 'none'
        lead_node_trace_elem.showlegend = False
        lead_node_trace_elem.marker.showscale = False
        lead_node_trace_elem.marker.reversescale = False
        lead_node_trace_elem.marker.color = lead_site_colors
        lead_node_trace_elem.marker.colorscale = \
                                    _p.convert_lead_cmap_mpl_plotly(lead_color,
                                                      [1, 1, 1, lead_color[3]])
        lead_node_trace_elem.marker.size = _p.convert_site_size_mpl_plotly(
                                        lead_site_size,
                                        defaults['plotly_site_size_reference'])

        if _p.isarray(lead_site_lw) or _p.isarray(lead_site_edgecolor):
            raise RuntimeError("Plotly engine not currently support an array "
                               "of linecolors or linewidths. Please restrict "
                               "to only a constant (i.e. no function or array) "
                               "lead_site_lw and lead_site_edgecolor property "
                               "for the entire plot.")
        lead_node_trace_elem.line.width = lead_site_lw
        lead_node_trace_elem.line.color = lead_site_edgecolor

        if lead_node_trace_elem:
            lead_node_trace.append(lead_node_trace_elem)

        lead_hop_colors = np.array([i[2] for i in hops[hops_slc]], dtype=float)

        end, start = end_pos[hops_slc], start_pos[hops_slc]

        if dim == 3:
            x0, y0, z0 = end.transpose()
            x1, y1, z1 = start.transpose()
            nones = [None] * len(x0)
            lead_edge_trace_elem = _p.plotly_graph_objs.Scatter3d(
                                        x=np.array([x0, x1, nones]).transpose().flatten(),
                                        y=np.array([y0, y1, nones]).transpose().flatten(),
                                        z=np.array([z0, z1, nones]).transpose().flatten()
                                    )

        else:
            x0, y0 = end.transpose()
            x1, y1 = start.transpose()
            nones = [None] * len(x0)
            lead_edge_trace_elem = _p.plotly_graph_objs.Scatter(
                                        x=np.array([x0, x1, nones]).transpose().flatten(),
                                        y=np.array([y0, y1, nones]).transpose().flatten()
                                    )

        lead_edge_trace_elem.line.width = lead_hop_lw
        lead_edge_trace_elem.line.color = _p.convert_colormap_mpl_plotly(*lead_color)
        lead_edge_trace_elem.hoverinfo = 'none'
        lead_edge_trace_elem.mode = 'lines'
        lead_edge_trace_elem.showlegend = False

        lead_edge_trace.append(lead_edge_trace_elem)

    layout = _p.plotly_graph_objs.Layout(
                                showlegend=False,
                                hovermode='closest',
                                xaxis=dict(showgrid=False, zeroline=False,
                                           showticklabels=True),
                                yaxis=dict(showgrid=False, zeroline=False,
                                           showticklabels=True))
    if fig is None:
        full_trace = list(itertools.chain.from_iterable([site_edge_trace,
                                            site_node_trace, lead_edge_trace,
                                            lead_node_trace]))
        fig = _p.plotly_graph_objs.Figure(data=full_trace,
                                          layout=layout)
    else:
        full_trace = list(itertools.chain.from_iterable([lead_edge_trace,
                                            lead_node_trace]))
        for trace in full_trace:
            try:
                fig.add_trace(trace)
            except TypeError:
                fig.data += [trace]

    return fig


def _plot_matplotlib(sys, num_lead_cells, unit,
         site_symbol, site_size,
         site_color, site_edgecolor, site_lw,
         hop_color, hop_lw,
         lead_site_symbol, lead_site_size, lead_color,
         lead_site_edgecolor, lead_site_lw,
         lead_hop_lw, pos_transform,
         cmap, colorbar, file,
         show, dpi, fig_size, ax):

    if unit is None:
        unit = 'nn'

    syst = sys  # for naming consistency inside function bodies
    # Generate data.
    sites, lead_sites_slcs = sys_leads_sites(syst, num_lead_cells)
    n_syst_sites = sum(i[1] is None for i in sites)
    sites_pos = sys_leads_pos(syst, sites)
    hops, lead_hops_slcs = sys_leads_hoppings(syst, num_lead_cells)
    n_syst_hops = sum(i[1] is None for i in hops)
    end_pos, start_pos = sys_leads_hopping_pos(syst, hops)

    loc = locals()

    for name in ['site_symbol', 'site_size', 'site_color', 'site_edgecolor',
                 'site_lw']:
        _check_length(name, loc)

    # Apply transformations to the data
    if pos_transform is not None:
        sites_pos = np.apply_along_axis(pos_transform, 1, sites_pos)
        end_pos = np.apply_along_axis(pos_transform, 1, end_pos)
        start_pos = np.apply_along_axis(pos_transform, 1, start_pos)

    dim = 3 if (sites_pos.shape[1] == 3) else 2
    if dim == 3 and not _p.has3d:
        raise RuntimeError("Installed matplotlib does not support 3d plotting")
    sites_pos = _resize_to_dim(sites_pos, dim)
    end_pos = _resize_to_dim(end_pos, dim)
    start_pos = _resize_to_dim(start_pos, dim)

    # Determine the reference length.
    if unit == 'pt':
        reflen = None
    elif unit == 'nn':
        if n_syst_hops:
            # If hoppings are present use their lengths to determine the
            # minimal one.
            distances = end_pos - start_pos
        else:
            # If no hoppings are present, use for the same purpose distances
            # from ten randomly selected points to the remaining points in the
            # system.
            points = _sample_array(sites_pos, 10).T
            distances = (sites_pos.reshape(1, -1, dim) -
                         points.reshape(-1, 1, dim)).reshape(-1, dim)
        distances = np.sort(np.sum(distances**2, axis=1))
        # Then check if distances are present that are way shorter than the
        # longest one. Then take first distance longer than these short
        # ones. This heuristic will fail for too large systems, or systems with
        # hoppings that vary by orders and orders of magnitude, but for sane
        # cases it will work.
        long_dist_coord = np.searchsorted(distances, 1e-16 * distances[-1])
        reflen = sqrt(distances[long_dist_coord])

    else:
        # The last allowed value is float-compatible.
        try:
            reflen = float(unit)
        except:
            raise ValueError('Invalid value of unit argument.')

    site_symbol = _make_proper_site_spec('site_symbol', site_symbol, syst, sites)
    if site_symbol is None: site_symbol = defaults['site_symbol'][dim]
    # separate different symbols (not done in 3D, the separation
    # would mess up sorting)
    if (_p.isarray(site_symbol) and dim != 3 and
        (len(site_symbol) != 3 or site_symbol[0] not in ('p', 'P'))):
        symbol_dict = defaultdict(list)
        for i, symbol in enumerate(site_symbol):
            symbol_dict[symbol].append(i)
        symbol_slcs = []
        for symbol, indx in symbol_dict.items():
            symbol_slcs.append((symbol, np.array(indx)))
        fancy_indexing = True
    else:
        symbol_slcs = [(site_symbol, slice(n_syst_sites))]
        fancy_indexing = False

    if site_color is None:
        cycle = _color_cycle()
        if isinstance(syst, (builder.FiniteSystem, builder.InfiniteSystem)):
            # Skipping the leads for brevity.
            families = sorted({site.family for site in syst.sites})
            color_mapping = dict(zip(families, cycle))
            def site_color(site):
                return color_mapping[syst.sites[site].family]
        elif isinstance(syst, builder.Builder):
            families = sorted({site[0].family for site in sites})
            color_mapping = dict(zip(families, cycle))
            def site_color(site):
                return color_mapping[site.family]
        else:
            # Unknown finalized system, no sites access.
            site_color = defaults['site_color'][dim]

    site_size = _make_proper_site_spec('site_size', site_size, syst, sites, fancy_indexing)
    site_color = _make_proper_site_spec('site_color', site_color, syst, sites, fancy_indexing)
    site_edgecolor = _make_proper_site_spec('site_edgecolor', site_edgecolor, syst, sites, fancy_indexing)
    site_lw = _make_proper_site_spec('site_lw', site_lw, syst, sites, fancy_indexing)

    hop_color = _make_proper_hop_spec(hop_color, hops)
    hop_lw = _make_proper_hop_spec(hop_lw, hops)

    # Choose defaults depending on dimension, if None was given
    if site_size is None: site_size = defaults['site_size'][dim]
    if site_edgecolor is None:
        site_edgecolor = defaults['site_edgecolor'][dim]
    if site_lw is None: site_lw = defaults['site_lw'][dim]

    if hop_color is None: hop_color = defaults['hop_color'][dim]
    if hop_lw is None: hop_lw = defaults['hop_lw'][dim]

    # if symbols are split up into different collections,
    # the colormapping will fail without normalization
    norm = None
    if len(symbol_slcs) > 1:
        try:
            if site_color.ndim == 1 and len(site_color) == n_syst_sites:
                site_color = np.asarray(site_color, dtype=float)
                norm = _p.matplotlib.colors.Normalize(site_color.min(),
                                                      site_color.max())
        except:
            pass

    # take spec also for lead, if it's not a list/array, default, otherwise
    if lead_site_symbol is None:
        lead_site_symbol = (site_symbol if not _p.isarray(site_symbol)
                            else defaults['site_symbol'][dim])
    if lead_site_size is None:
        lead_site_size = (site_size if not _p.isarray(site_size)
                          else defaults['site_size'][dim])
    if lead_color is None:
        lead_color = defaults['lead_color'][dim]
    lead_color = _p.matplotlib.colors.colorConverter.to_rgba(lead_color)

    if lead_site_edgecolor is None:
        lead_site_edgecolor = (site_edgecolor if not _p.isarray(site_edgecolor)
                               else defaults['site_edgecolor'][dim])
    if lead_site_lw is None:
        lead_site_lw = (site_lw if not _p.isarray(site_lw)
                        else defaults['site_lw'][dim])
    if lead_hop_lw is None:
        lead_hop_lw = (hop_lw if not _p.isarray(hop_lw)
                       else defaults['hop_lw'][dim])

    hop_cmap = None
    if not isinstance(cmap, str):
        try:
            cmap, hop_cmap = cmap
        except TypeError:
            pass

    # make a new figure unless axes specified
    if not ax:
        fig = _make_figure(dpi, fig_size, use_pyplot=(file is None))
        if dim == 2:
            ax = fig.add_subplot(1, 1, 1, aspect='equal')
            ax.set_xmargin(0.05)
            ax.set_ymargin(0.05)
        else:
            warnings.filterwarnings('ignore', message=r'.*rotation.*')
            ax = fig.add_subplot(1, 1, 1, projection='3d')
            warnings.resetwarnings()
    else:
        fig = None

    # plot system sites and hoppings
    for symbol, slc in symbol_slcs:
        size = site_size[slc] if _p.isarray(site_size) else site_size
        col = site_color[slc] if _p.isarray(site_color) else site_color
        edgecol = (site_edgecolor[slc] if _p.isarray(site_edgecolor) else
                   site_edgecolor)
        lw = site_lw[slc] if _p.isarray(site_lw) else site_lw

        symbol_coll = symbols(ax, sites_pos[slc], size=size,
                              reflen=reflen, symbol=symbol,
                              facecolor=col, edgecolor=edgecol,
                              linewidth=lw, cmap=cmap, norm=norm, zorder=2)

    end, start = end_pos[: n_syst_hops], start_pos[: n_syst_hops]
    line_coll = lines(ax, end, start, reflen, hop_color, linewidths=hop_lw,
                      zorder=1, cmap=hop_cmap)

    # plot lead sites and hoppings
    norm = _p.matplotlib.colors.Normalize(-0.5, num_lead_cells - 0.5)
    cmap_from_list = _p.matplotlib.colors.LinearSegmentedColormap.from_list
    lead_cmap = cmap_from_list(None, [lead_color, (1, 1, 1, lead_color[3])])

    for sites_slc, hops_slc in zip(lead_sites_slcs, lead_hops_slcs):
        lead_site_colors = np.array([i[2] for i in sites[sites_slc]],
                                    dtype=float)

        # Note: the previous version of the code had in addition this
        # line in the 3D case:
        # lead_site_colors = 1 / np.sqrt(1. + lead_site_colors)
        symbols(ax, sites_pos[sites_slc], size=lead_site_size, reflen=reflen,
                symbol=lead_site_symbol, facecolor=lead_site_colors,
                edgecolor=lead_site_edgecolor, linewidth=lead_site_lw,
                cmap=lead_cmap, zorder=2, norm=norm)

        lead_hop_colors = np.array([i[2] for i in hops[hops_slc]], dtype=float)

        # Note: the previous version of the code had in addition this
        # line in the 3D case:
        # lead_hop_colors = 1 / np.sqrt(1. + lead_hop_colors)
        end, start = end_pos[hops_slc], start_pos[hops_slc]
        lines(ax, end, start, reflen, lead_hop_colors, linewidths=lead_hop_lw,
              cmap=lead_cmap, norm=norm, zorder=1)

    min_ = np.min(sites_pos, 0)
    max_ = np.max(sites_pos, 0)
    m = (min_ + max_) / 2
    if dim == 2:
        w = np.max([(max_ - min_) / 2, (reflen, reflen)], axis=0)
        ax.update_datalim((m - w, m + w))
        ax.autoscale_view(tight=True)
    else:
        # make axis limits the same in all directions
        # (3D only works decently for equal aspect ratio. Since
        #  this doesn't work out of the box in mplot3d, this is a
        #  workaround)
        w = np.max(max_ - min_) / 2
        ax.auto_scale_xyz(*[(i - w, i + w) for i in m], had_data=True)

    # add separate colorbars for symbols and hoppings if ncessary
    if symbol_coll.get_array() is not None and colorbar and fig is not None:
        fig.colorbar(symbol_coll)
    if line_coll.get_array() is not None and colorbar and fig is not None:
        fig.colorbar(line_coll)

    return fig


def mask_interpolate(coords, values, a=None, method='nearest', oversampling=3):
    """Interpolate a scalar function in vicinity of given points.

    Create a masked array corresponding to interpolated values of the function
    at points lying not further than a certain distance from the original
    data points provided.

    Parameters
    ----------
    coords : np.ndarray
        An array with site coordinates.
    values : np.ndarray
        An array with the values from which the interpolation should be built.
    a : float, optional
        Reference length.  If not given, it is determined as a typical
        nearest neighbor distance.
    method : string, optional
        Passed to ``scipy.interpolate.griddata``: "nearest" (default), "linear",
        or "cubic"
    oversampling : integer, optional
        Number of pixels per reference length.  Defaults to 3.

    Returns
    -------
    array : ndarray
        The interpolated values.  The number of dimensions matches that of
        ``coords``.
    min, max : vectors
        The real-space coordinates of the two extreme ([0, 0] and [-1, -1])
        points of ``array``.

    Notes
    -----
    - `min` and `max` are chosen such that when plotting a system on a square
      lattice and `oversampling` is set to an odd integer, each site will lie
      exactly at the center of a pixel of the output array.

    - When plotting a system on a square lattice and `method` is "nearest", it
      makes sense to set `oversampling` to ``1``.  Then, each site will
      correspond to exactly one pixel in the resulting array.
    """
    # Build the bounding box.
    cmin, cmax = coords.min(0), coords.max(0)

    tree = spatial.cKDTree(coords)

    # Select 10 sites to compare -- comparing them all is too costly.
    points = _sample_array(coords, 10)
    min_dist = np.min(tree.query(points, 2)[0][:, 1])
    if min_dist < 1e-6 * np.linalg.norm(cmax - cmin):
        warnings.warn("Some sites have nearly coinciding positions, "
                      "interpolation may be confusing.",
                      RuntimeWarning, stacklevel=2)

    if a is None:
        a = min_dist

    if a < 1e-6 * np.linalg.norm(cmax - cmin):
        raise ValueError("The reference distance a is too small.")

    if len(coords) != len(values):
        raise ValueError("The number of sites doesn't match the number of "
                         "provided values.")

    shape = (((cmax - cmin) / a + 1) * oversampling).round()
    delta = 0.5 * (oversampling - 1) * a / oversampling
    cmin -= delta
    cmax += delta
    dims = tuple(slice(cmin[i], cmax[i], 1j * shape[i]) for i in
                 range(len(cmin)))
    grid = tuple(np.ogrid[dims])
    img = interpolate.griddata(coords, values, grid, method)
    img = img.astype(np.float64)
    mask = np.mgrid[dims].reshape(len(cmin), -1).T
    # The numerical values in the following line are optimized for the common
    # case of a square lattice:
    # * 0.99 makes sure that non-masked pixels and sites correspond 1-by-1 to
    #   each other when oversampling == 1.
    # * 0.4 (which is just below sqrt(2) - 1) makes tree.query() exact.
    mask = tree.query(mask, eps=0.4)[0] > 0.99 * a

    masked_result_array = np.ma.masked_array(img, mask)

    try:
        if _p.engine != "matplotlib":
            result_array = masked_result_array.filled(np.nan)
        else:
            result_array = masked_result_array
    except AttributeError:
        result_array = masked_result_array

    return result_array, img, cmin, cmax


def map(sys, value, colorbar=True, cmap=None, vmin=None, vmax=None, a=None,
        method='nearest', oversampling=3, num_lead_cells=0, file=None,
        show=True, dpi=None, fig_size=None, ax=None, pos_transform=None,
        background='#e0e0e0', *, interpolate=True):
    """Show map of a function defined for the sites of a 2D or 3D system.

    Create a pixmap representation of a function of the sites of a system by
    calling `~kwant.plotter.mask_interpolate` and show this pixmap using
    matplotlib.

    This function is similar to `~kwant.plotter.density`, but is more suited
    to the case where you want site-level resolution of the quantity that
    you are plotting. If your system has many sites you may get more appealing
    plots by using `~kwant.plotter.density`.

    Parameters
    ----------
    sys : kwant.system.FiniteSystem or kwant.builder.Builder
        The system for whose sites `value` is to be plotted.
    value : function or list
        Function which takes a site and returns a value if the system is a
        builder, or a list of function values for each system site of the
        finalized system.
    colorbar : bool, optional
        Whether to show a color bar if numerical data has to be plotted.
        Defaults to `True`. If `ax` is provided, the colorbar is never plotted.
    cmap : ``matplotlib`` color map or `None`
        The color map used for sites and optionally hoppings, if `None`,
        ``matplotlib`` default is used.
    vmin : float, optional
        The lower saturation limit for the colormap; values returned by
        `value` which are smaller than this will saturate
    vmax : float, optional
        The upper saturation limit for the colormap; valued returned by
        `value` which are larger than this will saturate
    a : float, optional
        Reference length.  If not given, it is determined as a typical
        nearest neighbor distance.
    method : string, optional
        Passed to ``scipy.interpolate.griddata``: "nearest" (default), "linear",
        or "cubic"
    oversampling : integer, optional
        Number of pixels per reference length.  Defaults to 3.
    num_lead_cells : integer, optional
        number of lead unit cells that should be plotted to indicate
        the position of leads. Defaults to 0.
    file : string or file object or `None`
        The output file.  If `None`, output will be shown instead.
    show : bool
        Whether ``matplotlib.pyplot.show()`` is to be called, and the output is
        to be shown immediately.  Defaults to `True`.
    ax : ``matplotlib.axes.Axes`` instance or `None`
        If `ax` is not `None`, no new figure is created, but the plot is done
        within the existing Axes `ax`. in this case, `file`, `show`, `dpi`
        and `fig_size` are ignored.
    pos_transform : function or `None`
        Transformation to be applied to the site position.
    background : matplotlib color spec
        Areas without sites are filled with this color.
    interpolate : bool, optional
        If ``True`` (default) the values are interpolated on a regular grid
        before plotting.  If ``False`` the values are shown directly on the
        site positions using ``kwant.plotter.plot``.

    Returns
    -------
    fig : matplotlib figure
        A figure with the output if `ax` is not set, else None.

    Notes
    -----
    - When plotting a system on a square lattice and `method` is "nearest", it
      makes sense to set `oversampling` to ``1``.  Then, each site will
      correspond to exactly one pixel.

    See Also
    --------
    kwant.plotter.density
    """

    if not (_p.mpl_available or _p.plotly_available):
        raise RuntimeError("matplotlib was not found, but is required "
                           "for map()")

    syst = sys  # for naming consistency inside function bodies
    sites = sys_leads_sites(syst, 0)[0]
    coords = sys_leads_pos(syst, sites)

    if pos_transform is not None:
        coords = np.apply_along_axis(pos_transform, 1, coords)

    dim = coords.shape[1]
    if dim not in (2, 3):
        raise ValueError('Only 2D or 3D systems can be plotted this way.')

    if callable(value):
        value = [value(site[0]) for site in sites]
    else:
        if not isinstance(syst, system.FiniteSystem):
            raise ValueError('List of values is only allowed as input '
                             'for finalized systems.')
    value = np.array(value)

    if interpolate:
        with _common.reraise_warnings():
            img, unmasked_data, _min, _max = mask_interpolate(coords, value,
                                                           a, method, oversampling)

        # Calculate the min/max bounds for the colormap.
        # User-provided values take precedence.
        if _p.engine != "matplotlib":
            unmasked_data = img.ravel()
        else:
            unmasked_data = img[~img.mask].data.flatten()
        unmasked_data = unmasked_data[~np.isnan(unmasked_data)]
        new_vmin, new_vmax = percentile_bound(unmasked_data, vmin, vmax)
        overflow_pct = 100 * np.sum(unmasked_data > new_vmax) / len(unmasked_data)
        underflow_pct = 100 * np.sum(unmasked_data < new_vmin) / len(unmasked_data)
        if (vmin is None and underflow_pct) or (vmax is None and overflow_pct):
            msg = (
                'The plotted data contains ',
                '{:.2f}% of values overflowing upper limit {:g} '
                    .format(overflow_pct, new_vmax)
                    if overflow_pct > 0 else '',
                'and ' if overflow_pct > 0 and underflow_pct > 0 else '',
                '{:.2f}% of values underflowing lower limit {:g} '
                    .format(underflow_pct, new_vmin)
                    if underflow_pct > 0 else '',
            )
            warnings.warn(''.join(msg), RuntimeWarning, stacklevel=2)
        vmin, vmax = new_vmin, new_vmax

        if _p.engine == "matplotlib":
            if dim == 3:
                fig = _map_matplotlib_3d(syst, img, colorbar, _max, _min, vmin, vmax,
                                     overflow_pct, underflow_pct, cmap, num_lead_cells,
                                     background, dpi, fig_size, ax, file)
            else:
                fig = _map_matplotlib(syst, img, colorbar, _max, _min,  vmin, vmax,
                                overflow_pct, underflow_pct, cmap, num_lead_cells,
                                background, dpi, fig_size, ax, file)
        elif _p.engine == "plotly":
            if dim == 3:
                fig = _map_plotly_3d(syst, img, colorbar, _max, _min, vmin, vmax,
                                   overflow_pct, underflow_pct, cmap, num_lead_cells,
                                   background)
            else:
                fig = _map_plotly(syst, img, colorbar, _max, _min,  vmin, vmax,
                                  overflow_pct, underflow_pct, cmap, num_lead_cells,
                                  background)
        elif _p.engine is None:
            raise RuntimeError("Cannot use map() without a plotting lib installed")
        else:
            raise RuntimeError("map() does not support engine '{}'".format(_p.engine))

    else:
        if _p.engine is None:
            raise RuntimeError("Cannot use map() without a plotting lib installed")
        fig = plot(syst, num_lead_cells=num_lead_cells, unit='pt',
                   site_symbol='o', site_size=None,
                   site_color=value, hop_lw=0,
                   cmap=cmap, colorbar=colorbar, file=None, show=False,
                   dpi=dpi, fig_size=fig_size, ax=ax,
                   lead_site_symbol='s', lead_site_size=0.501,
                   lead_hop_lw=0, lead_site_lw=0,
                   lead_color='black', lead_site_edgecolor=None,
                   pos_transform=pos_transform)

    _maybe_output_fig(fig, file=file, show=show)

    return fig


def _map_plotly(syst, img, colorbar, _max, _min,  vmin, vmax, overflow_pct,
                underflow_pct, cmap, num_lead_cells, background):

    border = 0.5 * (_max - _min) / (np.asarray(img.shape) - 1)
    _min -= border
    _max += border

    if cmap is None:
        cmap = _p.kwant_red_plotly

    img = img.T
    contour_object = _p.plotly_graph_objs.Heatmap()
    contour_object.z = img
    contour_object.x = np.linspace(_min[0],_max[0],img.shape[0])
    contour_object.y = np.linspace(_min[1],_max[1],img.shape[1])
    contour_object.zsmooth = False
    contour_object.connectgaps = False
    cmap = _p.convert_cmap_list_mpl_plotly(cmap)
    contour_object.colorscale = cmap
    contour_object.zmax = vmax
    contour_object.zmin = vmin
    contour_object.hoverinfo = 'none'

    contour_object.showscale = colorbar

    fig = _p.plotly_graph_objs.Figure(data=[contour_object])
    fig.layout.plot_bgcolor = background
    fig.layout.showlegend = False

    if num_lead_cells:
        fig = _plot_plotly(syst, num_lead_cells, site_symbol='no symbol',
                           hop_lw=0, lead_site_symbol='s',
                           lead_site_size=0.501, lead_site_lw=0,lead_hop_lw=0,
                           lead_color='black', colorbar=False, show=False,
                           fig=fig, unit='pt', site_size=None, site_color=None,
                           site_edgecolor=None, site_lw=0, hop_color=None,
                           lead_site_edgecolor=None,pos_transform=None,
                           cmap=None, file=None)

    return fig


def _map_plotly_3d(syst, img, colorbar, _max, _min, vmin, vmax, overflow_pct,
                   underflow_pct, cmap, num_lead_cells, background):

    border = 0.5 * (_max - _min) / (np.asarray(img.shape) - 1)
    _min -= border
    _max += border

    if cmap is None:
        cmap = _p.kwant_red_plotly

    xs = np.linspace(_min[0], _max[0], img.shape[0])
    ys = np.linspace(_min[1], _max[1], img.shape[1])
    zs = np.linspace(_min[2], _max[2], img.shape[2])
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing='ij')

    values = img
    if isinstance(values, np.ma.MaskedArray):
        mask = ~values.mask
        X = X[mask]
        Y = Y[mask]
        Z = Z[mask]
        values = values[mask]
    else:
        X = X.ravel()
        Y = Y.ravel()
        Z = Z.ravel()
        values = values.ravel()

    scatter = _p.plotly_graph_objs.Scatter3d(
        x=X, y=Y, z=Z, mode='markers',
        marker=dict(size=2,
                    color=values,
                    colorscale=_p.convert_cmap_list_mpl_plotly(cmap),
                    cmin=vmin, cmax=vmax,
                    showscale=colorbar))

    fig = _p.plotly_graph_objs.Figure(data=[scatter])
    fig.layout.scene.bgcolor = background
    fig.layout.showlegend = False

    if num_lead_cells:
        fig = _plot_plotly(syst, num_lead_cells, site_symbol='no symbol',
                           hop_lw=0, lead_site_symbol='s',
                           lead_site_size=0.501, lead_site_lw=0,
                           lead_hop_lw=0, lead_color='black', colorbar=False,
                           show=False, fig=fig, unit='pt', site_size=None,
                           site_color=None, site_edgecolor=None, site_lw=0,
                           hop_color=None, lead_site_edgecolor=None,
                           pos_transform=None, cmap=None, file=None)

    return fig


def _map_matplotlib(syst, img, colorbar, _max, _min,  vmin, vmax,
                    overflow_pct, underflow_pct, cmap, num_lead_cells,
                    background, dpi, fig_size, ax, file):

    border = 0.5 * (_max - _min) / (np.asarray(img.shape) - 1)
    _min -= border
    _max += border
    if ax is None:
        fig = _make_figure(dpi, fig_size, use_pyplot=(file is None))
        ax = fig.add_subplot(1, 1, 1, aspect='equal')
    else:
        fig = None

    if cmap is None:
        cmap = _p.kwant_red_matplotlib

    # Note that we tell imshow to show the array created by mask_interpolate
    # faithfully and not to interpolate by itself another time.
    image = ax.imshow(img.T, extent=(_min[0], _max[0], _min[1], _max[1]),
                      origin='lower', interpolation='none', cmap=cmap,
                      vmin=vmin, vmax=vmax)
    if num_lead_cells:
        plot(syst, num_lead_cells, site_symbol='no symbol', hop_lw=0,
             lead_site_symbol='s', lead_site_size=0.501, lead_site_lw=0,
             lead_hop_lw=0, lead_color='black', colorbar=False, ax=ax)

    ax.patch.set_facecolor(background)

    if colorbar and fig is not None:
        # Make the colorbar ends pointy if we saturate the colormap
        extend = 'neither'
        if underflow_pct > 0 and overflow_pct > 0:
            extend = 'both'
        elif underflow_pct > 0:
            extend = 'min'
        elif overflow_pct > 0:
            extend = 'max'
        fig.colorbar(image, extend=extend)

    return fig


def _map_matplotlib_3d(syst, img, colorbar, _max, _min, vmin, vmax,
                       overflow_pct, underflow_pct, cmap, num_lead_cells,
                       background, dpi, fig_size, ax, file):

    border = 0.5 * (_max - _min) / (np.asarray(img.shape) - 1)
    _min -= border
    _max += border

    if ax is None:
        fig = _make_figure(dpi, fig_size, use_pyplot=(file is None))
        warnings.filterwarnings('ignore', message=r'.*rotation.*')
        ax = fig.add_subplot(1, 1, 1, projection='3d')
        warnings.resetwarnings()
    else:
        fig = None

    if cmap is None:
        cmap = _p.kwant_red_matplotlib

    xs = np.linspace(_min[0], _max[0], img.shape[0])
    ys = np.linspace(_min[1], _max[1], img.shape[1])
    zs = np.linspace(_min[2], _max[2], img.shape[2])
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing='ij')

    values = img
    if isinstance(values, np.ma.MaskedArray):
        mask = ~values.mask
        X = X[mask]
        Y = Y[mask]
        Z = Z[mask]
        values = values[mask]
    else:
        X = X.ravel()
        Y = Y.ravel()
        Z = Z.ravel()
        values = values.ravel()

    sc = ax.scatter(X, Y, Z, c=values, cmap=cmap, vmin=vmin, vmax=vmax, s=4)

    if num_lead_cells:
        plot(syst, num_lead_cells, site_symbol='no symbol', hop_lw=0,
             lead_site_symbol='s', lead_site_size=0.501, lead_site_lw=0,
             lead_hop_lw=0, lead_color='black', colorbar=False, ax=ax)

    ax.set_facecolor(background)

    min_ = np.min([_min, _max], axis=0)
    max_ = np.max([_min, _max], axis=0)
    m = (min_ + max_) / 2
    w = np.max(max_ - min_) / 2
    ax.auto_scale_xyz(*[(i - w, i + w) for i in m], had_data=True)

    if colorbar and fig is not None:
        fig.colorbar(sc)

    return fig


@deprecate_args
def bands(sys, args=(), momenta=65, file=None, show=True, dpi=None,
          fig_size=None, ax=None, *, params=None):
    """Plot band structure of a translationally invariant 1D system.

    Parameters
    ----------
    sys : kwant.system.InfiniteSystem
        A system bands of which are to be plotted.
    args : tuple, defaults to empty
        Positional arguments to pass to the ``hamiltonian`` method.
        Deprecated in favor of 'params' (and mutually exclusive with it).
    momenta : int or 1D array-like
        Either a number of sampling points on the interval [-pi, pi], or an
        array of points at which the band structure has to be evaluated.
    file : string or file object or `None`
        The output file.  If `None`, output will be shown instead. If plotly is
        selected as the engine, the filename has to end with a html extension.
    show : bool
        For matplotlib engine, whether ``matplotlib.pyplot.show()`` is to be
        called, and the output is to be shown immediately.
        For the plotly engine, a call to ``iplot(fig)`` is made if
        show is True.
        Defaults to `True` for both engines.
    dpi : float
        Number of pixels per inch.  If not set the ``matplotlib`` default is
        used.
        Only for matplotlib engine. If the plotly engine is selected and
        this argument is not None, then a RuntimeError will be triggered.
    fig_size : tuple
        Figure size `(width, height)` in inches.  If not set, the default
        ``matplotlib`` value is used.
        Only for matplotlib engine. If the plotly engine is selected and
        this argument is not None, then a RuntimeError will be triggered.
    ax : ``matplotlib.axes.Axes`` instance or `None`
        If `ax` is not `None`, no new figure is created, but the plot is done
        within the existing Axes `ax`. in this case, `file`, `show`, `dpi`
        and `fig_size` are ignored.
        Only for matplotlib engine. If the plotly engine is selected and
        this argument is not None, then a RuntimeError will be triggered.
    params : dict, optional
        Dictionary of parameter names and their values. Mutually exclusive
        with 'args'.

    Returns
    -------
    fig : matplotlib figure or plotly Figure object
        A figure with the output if `ax` is not set, else None.

    Notes
    -----
    See `~kwant.physics.Bands` for the calculation of dispersion without plotting.
    """

    syst = sys  # for naming consistency inside function bodies

    if _p.plotly_available:
        if _p.engine == "plotly":
            _check_incompatible_args_plotly(dpi, fig_size, ax)

    _common.ensure_isinstance(syst, system.InfiniteSystem)

    momenta = np.array(momenta)
    if momenta.ndim != 1:
        momenta = np.linspace(-np.pi, np.pi, momenta)

    # expand out the contents of 'physics.Bands' to get the H(k),
    # because 'spectrum' already does the diagonalisation.
    ham = syst.cell_hamiltonian(args, params=params)
    if not np.allclose(ham, ham.conjugate().transpose()):
        raise ValueError('The cell Hamiltonian is not Hermitian.')
    _hop = syst.inter_cell_hopping(args, params=params)
    hop = np.empty(ham.shape, dtype=complex)
    hop[:, :_hop.shape[1]] = _hop
    hop[:, _hop.shape[1]:] = 0

    def h_k(k):
        # H_k = H_0 + V e^-ik + V^\dagger e^ik
        mat = hop * cmath.exp(-1j * k)
        mat +=  mat.conjugate().transpose() + ham
        return mat

    return spectrum(h_k, ('k', momenta), file=file, show=show, dpi=dpi,
                    fig_size=fig_size, ax=ax)


def spectrum(syst, x, y=None, params=None, mask=None, file=None,
             show=True, dpi=None, fig_size=None, ax=None):
    """Plot the spectrum of a Hamiltonian as a function of 1 or 2 parameters.

    This function requires either matplotlib or plotly to be installed.
    The default engine uses matplotlib for plotting.

    Parameters
    ----------
    syst : `kwant.system.FiniteSystem` or callable
        If a function, then it must take named parameters and return the
        Hamiltonian as a dense matrix.
    x : pair ``(name, values)``
        Parameter to ``ham`` that will be varied. Consists of the
        parameter name, and a sequence of parameter values.
    y : pair ``(name, values)``, optional
        Used for 3D plots (same as ``x``). If provided, then the cartesian
        product of the ``x`` values and these values will be used as a grid
        over which to evaluate the spectrum.
    params : dict, optional
        The rest of the parameters to ``ham``, which will be kept constant.
    mask : callable, optional
        Takes the parameters specified by ``x`` and ``y`` and returns True
        if the spectrum should not be calculated for the given parameter
        values.
    file : string or file object or `None`
        The output file.  If `None`, output will be shown instead. If plotly is
        selected as the engine, the filename has to end with a html extension.
    show : bool
        For matplotlib engine, whether ``matplotlib.pyplot.show()`` is to be
        called, and the output is to be shown immediately.
        For the plotly engine, a call to ``iplot(fig)`` is made if
        show is True.
        Defaults to `True` for both engines.
    dpi : float
        Number of pixels per inch.  If not set the ``matplotlib`` default is
        used.
        Only for matplotlib engine. If the plotly engine is selected and
        this argument is not None, then a RuntimeError will be triggered.
    fig_size : tuple
        Figure size `(width, height)` in inches.  If not set, the default
        ``matplotlib`` value is used.
        Only for matplotlib engine. If the plotly engine is selected and
        this argument is not None, then a RuntimeError will be triggered.
    ax : ``matplotlib.axes.Axes`` instance or `None`
        If `ax` is not `None`, no new figure is created, but the plot is done
        within the existing Axes `ax`. in this case, `file`, `show`, `dpi`
        and `fig_size` are ignored.
        Only for matplotlib engine. If the plotly engine is selected and
        this argument is not None, then a RuntimeError will be triggered.

    Returns
    -------
    fig : matplotlib figure or plotly Figure object
    """

    params = params or dict()

    if _p.engine == "matplotlib":
        return _spectrum_matplotlib(syst, x, y, params, mask, file,
                                    show, dpi, fig_size, ax)
    elif _p.engine == "plotly":
        _check_incompatible_args_plotly(dpi, fig_size, ax)
        return _spectrum_plotly(syst, x, y, params, mask, file, show)
    elif _p.engine is None:
        raise RuntimeError("Cannot use spectrum() without a plotting lib installed")
    else:
        raise RuntimeError("spectrum() does not support engine '{}'".format(_p.engine))


def _generate_spectrum(syst, params, mask, x, y):
    """Generates the spectrum dataset for the internal plotting
    functions of spectrum().

    Parameters
    ----------
    See spectrum(...) documentation.

    Returns
    -------
    spectrum : Numpy array
         The energies of the system calculated at each coordinate.
    planar : bool
         True if y is None
    array_values : tuple
         The coordinates of x, y values of the dataset for plotting.
    keys : tuple
         Labels for the x and y axes.
    """

    if isinstance(syst, system.FiniteSystem):
        def ham(**kwargs):
            return syst.hamiltonian_submatrix(params=kwargs, sparse=False)
    elif callable(syst):
        ham = syst
    else:
        raise TypeError("Expected 'syst' to be a finite Kwant system "
                        "or a function.")

    planar = y is None
    keys = (x[0],) if planar else (x[0], y[0])
    array_values = (x[1],) if planar else (x[1], y[1])

    # calculate spectrum on the grid of points
    spectrum = []
    bound_ham = functools.partial(ham, **params)
    for point in itertools.product(*array_values):
        p = dict(zip(keys, point))
        if mask and mask(**p):
            spectrum.append(None)
        else:
            h_p = np.atleast_2d(bound_ham(**p))
            spectrum.append(np.linalg.eigvalsh(h_p))
    # massage masked grid points into a list of NaNs of the appropriate length
    shape_eigvals = next(filter(lambda s: s is not None, spectrum)).shape
    nan_list = np.full(shape_eigvals, np.nan)
    spectrum = [nan_list if s is None else s for s in spectrum]
    # make into a numpy array and reshape
    new_shape = [len(v) for v in array_values] + [-1]
    spectrum = np.array(spectrum).reshape(new_shape)

    return spectrum, planar, array_values, keys


def _spectrum_plotly(syst, x, y=None, params=None, mask=None,
                     file=None, show=True):
    """Plot the spectrum of a Hamiltonian as a function of 1 or 2 parameters
    using the plotly engine.

    Parameters
    ----------
    See spectrum(...) documentation.

    Returns
    -------
    fig : plotly Figure / dict
    """

    spectrum, planar, array_values, keys = _generate_spectrum(syst, params,
                                                              mask, x, y)

    if planar:
        fig = _p.plotly_graph_objs.Figure(data=[
          _p.plotly_graph_objs.Scatter(
                 x=array_values[0],
                 y=energies,
          ) for energies in spectrum.T
        ])
        fig.layout.xaxis.title = keys[0]
        fig.layout.yaxis.title = 'Energy'
        fig.layout.showlegend = False
    else:
        fig = _p.plotly_graph_objs.Figure(data=[
          _p.plotly_graph_objs.Surface(
                 x=array_values[0],
                 y=array_values[1],
                 z=energies,
                 cmax=np.max(spectrum),
                 cmin=np.min(spectrum),
          ) for energies in spectrum.T
        ])
        fig.layout.scene.xaxis.title = keys[0]
        fig.layout.scene.yaxis.title = keys[1]
        fig.layout.scene.zaxis.title = 'Energy'

    fig.layout.title = (
        ', '.join('{} = {}'.format(*kv) for kv in params.items())
    )

    _maybe_output_fig(fig, file=file, show=show)

    return fig


def _spectrum_matplotlib(syst, x, y=None, params=None, mask=None, file=None,
                         show=True, dpi=None, fig_size=None, ax=None):
    """Plot the spectrum of a Hamiltonian as a function of 1 or 2 parameters
    using the matplotlib engine.

    Parameters
    ----------
    See spectrum(...) documentation.

    Returns
    -------
    fig : matplotlib figure
        A figure with the output if `ax` is not set, else None.
    """

    if y is not None and not _p.has3d:
        raise RuntimeError("Installed matplotlib does not support 3d plotting")

    spectrum, planar, array_values, keys = _generate_spectrum(syst, params,
                                                              mask, x, y)

    # set up axes
    if ax is None:
        fig = _make_figure(dpi, fig_size, use_pyplot=(file is None))
        if planar:
            ax = fig.add_subplot(1, 1, 1)
        else:
            warnings.filterwarnings('ignore',
                                    message=r'.*mouse rotation disabled.*')
            ax = fig.add_subplot(1, 1, 1, projection='3d')
            warnings.resetwarnings()
        ax.set_xlabel(keys[0])
        if planar:
            ax.set_ylabel('Energy')
        else:
            ax.set_ylabel(keys[1])
            ax.set_zlabel('Energy')
        ax.set_title(
            ', '.join(
                '{} = {}'.format(key, value)
                for key, value in params.items()
                if not callable(value)
            )
        )
    else:
        fig = None

    # actually do the plot
    if planar:
        ax.plot(array_values[0], spectrum)
    else:
        if not hasattr(ax, 'plot_surface'):
            msg = ("When providing an axis for plotting over a 2D domain the "
                   "axis should be created with 'projection=\"3d\"")
            raise TypeError(msg)
        # plot_surface cannot directly handle rank-3 values, so we
        # explicitly loop over the last axis
        grid = np.meshgrid(*array_values)
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='Z contains NaN values')
            for i in range(spectrum.shape[-1]):
                spec = spectrum[:, :, i].transpose()  # row-major to x-y ordering
                ax.plot_surface(*(grid + [spec]), cstride=1, rstride=1)

    _maybe_output_fig(fig, file=file, show=show)

    return fig


# Smoothing functions used with 'interpolate_current'.

# Convolution kernel with finite support:
# f(r) = (1-r^2)^2 Θ(1-r^2)
def _bump(r):
    r[r > 1] = 1
    m = 1 - r * r
    return m * m


# We generate the smoothing function by convolving the current
# defined on a line between the two sites with
# f(ρ, z) = (1 - ρ^2 - z^2)^2 Θ(1 - ρ^2 - z^2), where ρ and z are
# cylindrical coords defined with respect to the hopping.
# 'F' is the result of the convolution.
def _smoothing(rho, z):
    r = 1 - rho * rho
    r[r < 0] = 0
    r = np.sqrt(r)
    m = np.clip(z, -r, r)
    rr = r * r
    rrrr = rr * rr
    mm = m * m
    return m * (mm * (mm/5 - (2/3) * rr) + rrrr) + (8 / 15) * rrrr * r


# We need to normalize the smoothing function so that it has unit cross
# section in the plane perpendicular to the hopping. This is equivalent
# to normalizing the integral of 'f' over the unit hypersphere to 1.
# The smoothing function goes as F(ρ) = (16/15) (1 - ρ^2)^(5/2) in the
# plane perpendicular to the hopping, so the cross section is:
# A_n = (16 / 15) * σ_n * ∫_0^1 ρ^(n-1) (1 - ρ^2)^(5/2) dρ
# where σ_n is the surface element prefactor (2 in 2D, 2π in 3D). Rather
# that calculate A_n every time, we hard code its value for 1, 2 and 3D.
_smoothing_cross_sections = [16 / 15, np.pi / 3, 32 * np.pi / 105]


# Determine the optimal bump function width from the absolute and
# relative widths provided, and the lengths of all the hoppings in the system
def _optimal_width(lens, abswidth, relwidth, bbox_size):
    if abswidth is None:
        if relwidth is None:
            unique_lens = np.unique(lens)
            longest = unique_lens[-1]
            for shortest_nonzero in unique_lens:
                if shortest_nonzero / longest > 1e-3:
                    break
            width = 4 * shortest_nonzero
        else:
            width = relwidth * np.max(bbox_size)
    else:
        width = abswidth

    return width


# Create empty field array that covers the bounding box plus
# some additional padding
def _create_field(dim, bbox_size, width, n, is_current):
    field_shape = np.zeros(dim + 1, int)
    field_shape[dim] = dim if is_current else 1
    for d in range(dim):
        field_shape[d] = int(bbox_size[d] * n / width + n)
        if field_shape[d] % 2:
            field_shape[d] += 1
    field = np.zeros(field_shape)
    # padding is width / 2
    return field, width / 2


def density_kernel(coords):
    r = np.sqrt(np.sum(coords * coords))
    return _bump(r)[..., None]


def current_kernel(coords, direction, length):
    z = np.dot(coords, direction)
    rho = np.sqrt(np.abs(np.sum(coords * coords) - z * z))
    magn = (_smoothing(rho, z) - _smoothing(rho, z - length))
    return direction * magn[..., None]


# interpolate a discrete scalar or vector field.
def _interpolate_field(dim, elements, discrete_field, bbox, width,
                       padding, field_out):

    field_shape = np.array(field_out.shape)
    bbox_min, bbox_max = bbox

    scale = 2 / width

    # if density elements is shape (nsites, dim)
    # if current elements is shape (nhops, 2, dim)
    assert elements.shape[-1] == dim
    is_current = len(elements.shape) == 3
    if is_current:
        assert elements.shape[1] == 2
        dirs = elements[:, 1] - elements[:, 0]
        lens = np.sqrt(np.sum(dirs * dirs, axis=-1))
        dirs /= lens[:, None]
        dirs = np.nan_to_num(dirs)
        lens = lens * scale

    if is_current:
        pos_offsets = elements[:, 0]  # first site in hopping
        kernel = current_kernel
    else:
        pos_offsets = elements  # sites themselves
        kernel = density_kernel

    region = [np.linspace(bbox_min[d] - padding,
                          bbox_max[d] + padding,
                          field_shape[d])
              for d in range(dim)]

    grid_density = (field_shape[:dim] - 1) / (bbox_max + 2*padding - bbox_min)

    # slices for indexing 'field' and 'region' array
    slices = np.empty((len(discrete_field), dim, 2), int)
    if is_current:
        mn = np.min(elements, 1)
        mx = np.max(elements, 1)
    else:
        mn = mx = elements
    slices[:, :, 0] = np.floor((mn - bbox_min) * grid_density)
    slices[:, :, 1] = np.ceil((mx + 2*padding - bbox_min) * grid_density)

    for i in range(len(discrete_field)):

        if not np.diff(slices[i]).all() or not discrete_field[i]:
            # Zero volume or zero field: nothing to do.
            continue

        field_slice = tuple([slice(*slices[i, d]) for d in range(dim)])

        # Coordinates of the grid points that are within range of the current
        # hopping.
        coords = np.array(
            np.meshgrid(
                *[region[d][field_slice[d]] for d in range(dim)],
                sparse=True, indexing='ij'
            ),
            dtype=object
        )

        # Convert "coords" into scaled distances from pos_offset
        coords -= pos_offsets[i]
        coords *= scale
        magns = kernel(coords, dirs[i], lens[i]) if is_current else kernel(coords)
        magns *= discrete_field[i]

        field_out[field_slice] += magns

    field_out *= scale / _smoothing_cross_sections[dim - 1]


def interpolate_current(syst, current, relwidth=None, abswidth=None, n=9):
    """Interpolate currents in a system onto a regular grid.

    The system graph together with current intensities defines a "discrete"
    current density field where the current density is non-zero only on the
    straight lines that connect sites that are coupled by a hopping term.

    To make this vector field easier to visualize and interpret at different
    length scales, it is smoothed by convoluting it with the bell-shaped bump
    function ``f(r) = max(1 - (2*r / width)**2, 0)**2``.  The bump width is
    determined by the `relwidth` and `abswidth` parameters.

    This routine samples the smoothed field on a regular (square or cubic)
    grid.

    Parameters
    ----------
    syst : A finalized system
        The system on which we are going to calculate the field.
    current : '1D array of float'
        Must contain the intensity on each hoppings in the same order that they
        appear in syst.graph.
    relwidth : float or `None`
        Relative width of the bumps used to generate the field, as a fraction
        of the length of the longest side of the bounding box.  This argument
        is only used if `abswidth` is not given.
    abswidth : float or `None`
        Absolute width of the bumps used to generate the field.  Takes
        precedence over `relwidth`.  If neither is given, the bump width is set
        to four times the length of the shortest hopping.
    n : int
        Number of points the grid must have over the width of the bump.

    Returns
    -------
    field : n-d arraylike of float
        n-d array of n-d vectors.
    box : sequence of 2-sequences of float
        the extents of `field`: ((x0, x1), (y0, y1), ...)

    """
    if not isinstance(syst, builder.FiniteSystem):
        raise TypeError("The system needs to be finalized.")

    if len(current) != syst.graph.num_edges:
        raise ValueError("Current and hoppings arrays do not have the same"
                         " length.")

    # hops: hoppings (pairs of points)
    dim = len(syst.sites[0].pos)
    hops = np.empty((syst.graph.num_edges // 2, 2, dim))
    # Take the average of the current flowing each way along the hoppings
    current_one_way = np.empty(syst.graph.num_edges // 2)
    seen_hoppings = dict()
    kprime = 0
    for k, (i, j) in enumerate(syst.graph):
        if (j, i) in seen_hoppings:
            current_one_way[seen_hoppings[j, i]] -= current[k]
        else:
            current_one_way[kprime] = current[k]
            hops[kprime][0] = syst.sites[j].pos
            hops[kprime][1] = syst.sites[i].pos
            seen_hoppings[i, j] = kprime
            kprime += 1
    current = current_one_way / 2

    min_hops = np.min(hops, 1)
    max_hops = np.max(hops, 1)
    bbox_min = np.min(min_hops, 0)
    bbox_max = np.max(max_hops, 0)
    bbox_size = bbox_max - bbox_min

    # lens: scaled lengths of hoppings
    # dirs: normalized directions of hoppings
    dirs = hops[:, 1] - hops[:, 0]
    lens = np.sqrt(np.sum(dirs * dirs, -1))
    dirs /= lens[:, None]
    dirs = np.nan_to_num(dirs)
    width = _optimal_width(lens, abswidth, relwidth, bbox_size)


    field, padding = _create_field(dim, bbox_size, width, n, is_current=True)
    boundaries = tuple((bbox_min[d] - padding, bbox_max[d] + padding)
                        for d in range(dim))
    _interpolate_field(dim, hops, current,
                       (bbox_min, bbox_max), width, padding, field)

    return field, boundaries


def interpolate_density(syst, density, relwidth=None, abswidth=None, n=9,
                        mask=True):
    """Interpolate density in a system onto a regular grid.

    The system sites together with a scalar for each site defines a "discrete"
    density field where the density is non-zero only at the site positions.

    To make this vector field easier to visualize and interpret at different
    length scales, it is smoothed by convoluting it with the bell-shaped bump
    function ``f(r) = max(1 - (2*r / width)**2, 0)**2``.  The bump width is
    determined by the `relwidth` and `abswidth` parameters.

    This routine samples the smoothed field on a regular (square or cubic)
    grid.

    Parameters
    ----------
    syst : A finalized system
        The system on which we are going to calculate the field.
    density : 1D array of float
        Must contain the intensity on each site in the same order that they
        appear in syst.sites.
    relwidth : float, optional
        Relative width of the bumps used to smooth the field, as a fraction
        of the length of the longest side of the bounding box.  This argument
        is only used if ``abswidth`` is not given.
    abswidth : float, optional
        Absolute width of the bumps used to smooth the field.  Takes
        precedence over ``relwidth``.  If neither is given, the bump width is set
        to four times the length of the shortest hopping.
    n : int
        Number of points the grid must have over the width of the bump.
    mask : Bool
        If True, this function returns a masked array that masks positions that
        are too far away from any sites. This is useful for showing an approximate
        outline of the system when the field is plotted.

    Returns
    -------
    field : n-d arraylike of float
        n-d array of n-d vectors.
    box : sequence of 2-sequences of float
        the extents of ``field``: ((x0, x1), (y0, y1), ...)

    """
    if not isinstance(syst, builder.FiniteSystem):
        raise TypeError("The system needs to be finalized.")

    if len(density) != len(syst.sites):
        raise ValueError("Density and sites arrays do not have the same"
                         " length.")

    dim = len(syst.sites[0].pos)
    sites = np.array([s.pos for s in syst.sites])

    bbox_min = np.min(sites, axis=0)
    bbox_max = np.max(sites, axis=0)
    bbox_size = bbox_max - bbox_min

    # Determine the optimal width for the bump function
    dirs = np.array([syst.sites[i].pos - syst.sites[j].pos
                     for i, j in syst.graph])
    lens = np.sqrt(np.sum(dirs * dirs, -1))
    width = _optimal_width(lens, abswidth, relwidth, bbox_size)

    field, padding = _create_field(dim, bbox_size, width, n, is_current=False)
    boundaries = tuple((bbox_min[d] - padding, bbox_max[d] + padding)
                        for d in range(dim))
    _interpolate_field(dim, sites, density,
                       (bbox_min, bbox_max), width, padding, field)

    if mask:
        # Field is zero when we are > 0.5*width from any site (as bump has
        # finite support), so we mask positions a little further than this.
        field = _mask(field,
                      box=boundaries,
                      coords=np.array([s.pos for s in syst.sites]),
                      cutoff=0.6*width)

    return field, boundaries


def _gamma_compress(linear):
    """Compress linear sRGB into sRGB."""
    if linear <= 0.0031308:
        return 12.92 * linear
    else:
        a = 0.055
        return (1 + a) * linear ** (1 / 2.4) - a

_gamma_compress = np.vectorize(_gamma_compress, otypes=[float])


def _gamma_expand(corrected):
    """Expand sRGB into linear sRGB."""
    if corrected <= 0.04045:
        return corrected / 12.92
    else:
        a = 0.055
        return ((corrected + a) / (1 + a))**2.4

_gamma_expand = np.vectorize(_gamma_expand, otypes=[float])


def _linear_cmap(a, b):
    """Make a colormap that linearly interpolates between the colors a and b."""
    a = _p.matplotlib.colors.colorConverter.to_rgb(a)
    b = _p.matplotlib.colors.colorConverter.to_rgb(b)
    a_linear = _gamma_expand(a)
    b_linear = _gamma_expand(b)
    color_diff = a_linear - b_linear
    palette = (np.linspace(0, 1, 256).reshape((-1, 1))
               * color_diff.reshape((1, -1)))
    palette += b_linear
    palette = _gamma_compress(palette)
    return _p.matplotlib.colors.ListedColormap(palette)


def streamplot(field, box, cmap=None, bgcolor=None, linecolor='k',
               max_linewidth=3, min_linewidth=1, density=2/9,
               colorbar=True, file=None,
               show=True, dpi=None, fig_size=None, ax=None,
               vmax=None):
    if _p.engine == "matplotlib":
        fig = _streamplot_matplotlib(field, box, cmap, bgcolor, linecolor,
               max_linewidth, min_linewidth, density, colorbar, file,
               show, dpi, fig_size, ax, vmax)
    elif _p.engine == "plotly":
        _check_incompatible_args_plotly(dpi, fig_size, ax)
        fig = _streamplot_plotly(field, box, cmap, bgcolor, linecolor,
                               max_linewidth, min_linewidth, density,
                               colorbar, file, show, vmax)
    elif _p.engine is None:
        raise RuntimeError("Cannot use streamplot() without a plotting lib installed")
    else:
        raise RuntimeError("streamplot() does not support engine '{}'".format(_p.engine))
    _maybe_output_fig(fig, file=file, show=show)


def _streamplot_plotly(field, box, cmap, bgcolor, linecolor,
                               max_linewidth, min_linewidth, density,
                               colorbar, file, show, vmax):
    raise RuntimeError("Streamplot() for plotly engine not implemented yet due to bug from plotly")


def _streamplot_matplotlib(field, box, cmap, bgcolor, linecolor,
               max_linewidth, min_linewidth, density, colorbar, file,
               show, dpi, fig_size, ax, vmax):
    """Draw streamlines of a flow field in Kwant style

    Solid colored streamlines are drawn, superimposed on a color plot of
    the flow speed that may be disabled by setting `bgcolor`.  The width
    of the streamlines is proportional to the flow speed.  Lines that
    would be thinner than `min_linewidth` are blended in a perceptually
    correct way into the background color in order to create the
    illusion of arbitrarily thin lines.  (This is done because some plot
    engines like PDF do not support lines of arbitrarily thin width.)

    Internally, this routine uses matplotlib's streamplot.

    Parameters
    ----------
    field : 3d arraylike of float
        2d array of 2d vectors.
    box : 2-sequence of 2-sequences of float
        the extents of `field`: ((x0, x1), (y0, y1))
    cmap : colormap, optional
        Colormap for the background color plot.  When not set the colormap
        "kwant_red" is used by default, unless `bgcolor` is set.
    bgcolor : color definition, optional
        The solid color of the background.  Mutually exclusive with `cmap`.
    linecolor : color definition
        Color of the flow lines.
    max_linewidth : float
        Width of lines at maximum flow speed.
    min_linewidth : float
        Minimum width of lines before blending into the background color begins.
    density : float
        Number of flow lines per point of the field.  The default value
        of 2/9 is chosen to show two lines per default width of the
        interpolation bump of `~kwant.plotter.interpolate_current`.
    colorbar : bool
        Whether to show a colorbar if a colormap is used. Ignored if `ax` is
        provided.
    file : string or file object or `None`
        The output file.  If `None`, output will be shown instead.
    show : bool
        Whether ``matplotlib.pyplot.show()`` is to be called, and the output is
        to be shown immediately.  Defaults to `True`.
    dpi : float or `None`
        Number of pixels per inch.  If not set the ``matplotlib`` default is
        used.
    fig_size : tuple or `None`
        Figure size `(width, height)` in inches.  If not set, the default
        ``matplotlib`` value is used.
    ax : ``matplotlib.axes.Axes`` instance or `None`
        If `ax` is not `None`, no new figure is created, but the plot is done
        within the existing Axes `ax`. in this case, `file`, `show`, `dpi`
        and `fig_size` are ignored.
    vmax : float or `None`
        The upper saturation limit for the colormap; flows higher than
        this will saturate.  Note that there is no corresponding vmin
        option, vmin being fixed at zero.

    Returns
    -------
    fig : matplotlib figure
        A figure with the output if `ax` is not set, else None.
    """

    # Matplotlib's "density" is in units of 30 streamlines...
    density *= 1 / 30 * ta.array(field.shape[:2], int)

    # Matplotlib plots images like matrices: image[y, x].  We use the opposite
    # convention: image[x, y].  Hence, it is necessary to transpose.
    field = field.transpose(1, 0, 2)

    if field.shape[-1] != 2 or field.ndim != 3:
        raise ValueError("Only 2D field can be plotted.")

    if bgcolor is None:
        if cmap is None:
            cmap = _p.kwant_red_matplotlib
        if isinstance(cmap, str):
            cmap = _p.get_cmap(cmap)
        bgcolor = cmap(0)[:3]
    elif cmap is not None:
        raise ValueError("The parameters 'cmap' and 'bgcolor' are "
                         "mutually exclusive.")

    if ax is None:
        fig = _make_figure(dpi, fig_size, use_pyplot=(file is None))
        ax = fig.add_subplot(1, 1, 1, aspect='equal')
    else:
        fig = None

    X = np.linspace(*box[0], num=field.shape[1])
    Y = np.linspace(*box[1], num=field.shape[0])

    speed = np.linalg.norm(field, axis=-1)
    if vmax is None:
        vmax = np.max(speed) or 1

    if cmap is None:
        ax.set_axis_bgcolor(bgcolor)
    else:
        image = ax.imshow(speed, cmap=cmap,
                          interpolation='bicubic',
                          extent=[e for c in box for e in c],
                          origin='lower', vmin=0, vmax=vmax)

    linewidth = max_linewidth / vmax * speed
    color = linewidth / min_linewidth
    thin = linewidth < min_linewidth
    linewidth[thin] = min_linewidth
    color[~ thin] = 1

    line_cmap = _linear_cmap(linecolor, bgcolor)

    ax.streamplot(X, Y, field[:,:,0], field[:,:,1],
                  density=density, linewidth=linewidth,
                  color=color, cmap=line_cmap, arrowstyle='->',
                  norm=_p.matplotlib.colors.Normalize(0, 1))

    ax.set_xlim(*box[0])
    ax.set_ylim(*box[1])

    if colorbar and cmap and fig is not None:
        fig.colorbar(image)

    _maybe_output_fig(fig, file=file, show=show)

    return fig


def scalarplot(field, box,
               cmap=None, colorbar=True, file=None, show=True,
               dpi=None, fig_size=None, ax=None, vmin=None, vmax=None,
               background='#e0e0e0'):
    """Draw a scalar field in Kwant style

    Internally, this routine uses matplotlib's imshow.

    Parameters
    ----------
    field : 2d arraylike of float
        2d scalar field to plot.
    box : pair of pair of float
        the realspace extents of ``field``: ((x0, x1), (y0, y1))
    cmap : colormap, optional
        Colormap for the background color plot.  When not set the colormap
        "kwant_red" is used by default.
    colorbar : bool, default: True
        Whether to show a colorbar if a colormap is used. Ignored if `ax` is
        provided.
    file : string or file object, optional
        The output file.  If not provided, output will be shown instead.
    show : bool, default: True
        Whether ``matplotlib.pyplot.show()`` is to be called, and the output is
        to be shown immediately.
    dpi : float, optional
        Number of pixels per inch.  If not set the ``matplotlib`` default is
        used.
    fig_size : tuple, optional
        Figure size ``(width, height)`` in inches.  If not set, the default
        ``matplotlib`` value is used.
    ax : ``matplotlib.axes.Axes`` instance, optional
        If ``ax`` is provided, no new figure is created, but the plot is done
        within the existing Axes ``ax``. in this case, ``file``, ``show``,
        ``dpi`` and ``fig_size`` are ignored.
    vmin, vmax : float, optional
        The lower/upper saturation limit for the colormap.
    background : matplotlib color spec
        Areas outside the system are filled with this color.

    Returns
    -------
    fig : matplotlib figure
        A figure with the output if ``ax`` is not set, else None.
    """

    # Matplotlib plots images like matrices: image[y, x].  We use the opposite
    # convention: image[x, y].  Hence, it is necessary to transpose.
    # Also squeeze out the last axis as it is just a scalar field

    field = field.squeeze(axis=-1).transpose()

    if field.ndim != 2:
        raise ValueError("Only 2D field can be plotted.")

    if vmin is None:
        vmin = np.min(field)
    if vmax is None:
        vmax = np.max(field)

    if _p.engine == "matplotlib":
        fig = _scalarplot_matplotlib(field, box, cmap, colorbar,
                                     file, show, dpi, fig_size, ax,
                                     vmin, vmax, background)
    elif _p.engine == "plotly":
        _check_incompatible_args_plotly(dpi, fig_size, ax)
        fig = _scalarplot_plotly(field, box, cmap, colorbar, file,
                                 show, vmin, vmax, background)
    elif _p.engine is None:
        raise RuntimeError("Cannot use scalarplot() without a plotting lib installed")
    else:
        raise RuntimeError("scalarplot() does not support engine '{}'".format(_p.engine))
    _maybe_output_fig(fig, file=file, show=show)

    return fig


def _scalarplot_plotly(field, box, cmap, colorbar, file,
                       show, vmin, vmax, background):

    if cmap is None:
        cmap = _p.kwant_red_plotly

    contour_object = _p.plotly_graph_objs.Heatmap()
    contour_object.z = field
    contour_object.x = np.linspace(*box[0],field.shape[0])
    contour_object.y = np.linspace(*box[1],field.shape[1])
    contour_object.zsmooth = 'best'
    contour_object.colorscale = cmap
    contour_object.zmax = vmax
    contour_object.zmin = vmin

    contour_object.showscale = colorbar

    fig = _p.plotly_graph_objs.Figure(data=[contour_object])
    fig.layout.plot_bgcolor = background

    return fig


def _scalarplot_matplotlib(field, box, cmap, colorbar, file, show, dpi,
                           fig_size, ax, vmin, vmax, background):

    if cmap is None:
        cmap = _p.kwant_red_matplotlib
    if isinstance(cmap, str):
        cmap = _p.get_cmap(cmap)

    if ax is None:
        fig = _make_figure(dpi, fig_size, use_pyplot=(file is None))
        ax = fig.add_subplot(1, 1, 1, aspect='equal')
    else:
        fig = None

    image = ax.imshow(field, cmap=cmap,
                      interpolation='bicubic',
                      extent=[e for c in box for e in c],
                      origin='lower', vmin=vmin, vmax=vmax)

    ax.set_xlim(*box[0])
    ax.set_ylim(*box[1])
    ax.patch.set_facecolor(background)

    if colorbar and cmap and fig is not None:
        fig.colorbar(image)

    return fig


def current(syst, current, relwidth=0.05, **kwargs):
    """Show an interpolated current defined for the hoppings of a system.

    The system graph together with current intensities defines a "discrete"
    current density field where the current density is non-zero only on the
    straight lines that connect sites that are coupled by a hopping term.

    To make this scalar field easier to visualize and interpret at different
    length scales, it is smoothed by convoluting it with the bell-shaped bump
    function ``f(r) = max(1 - (2*r / width)**2, 0)**2``.  The bump width is
    determined by the ``relwidth`` parameter.

    This routine samples the smoothed field on a regular (square or cubic) grid
    and displays it using an enhanced variant of matplotlib's streamplot.

    This is a convenience function that is equivalent to
    ``streamplot(*interpolate_current(syst, current, relwidth), **kwargs)``.
    The longer form makes it possible to tweak additional options of
    `~kwant.plotter.interpolate_current`.

    Parameters
    ----------
    syst : `kwant.system.FiniteSystem`
        The system for which to plot the ``current``.
    current : sequence of float
        Sequence of values defining currents on each hopping of the system.
        Ordered in the same way as ``syst.graph``. This typically will be
        the result of evaluating a `~kwant.operator.Current` operator.
    relwidth : float or `None`
        Relative width of the bumps used to smooth the field, as a fraction
        of the length of the longest side of the bounding box.
    **kwargs : various
        Keyword args to be passed verbatim to `kwant.plotter.streamplot`.

    Returns
    -------
    fig : matplotlib figure
        A figure with the output if ``ax`` is not set, else None.

    See Also
    --------
    kwant.plotter.density
    """
    with _common.reraise_warnings(4):
        return streamplot(*interpolate_current(syst, current, relwidth),
                          **kwargs)


def _mask(field, box, coords, cutoff):
    tree = spatial.cKDTree(coords)

    # Build the mask initially as a 2D array
    dims = tuple(slice(boxmin, boxmax, 1j * shape)
                 for (boxmin, boxmax), shape in zip(box, field.shape))
    mask = np.mgrid[dims].reshape(len(box), -1).T

    mask = tree.query(mask, distance_upper_bound=cutoff)[0] == np.inf
    return np.ma.masked_array(field, mask)


def density(syst, density, relwidth=0.05, **kwargs):
    """Show an interpolated density defined on the sites of a system.

    The system sites, together with a scalar per site defines a "discrete"
    density field that is non-zero only on the sites.

    To make this scalar field easier to visualize and interpret at different
    length scales, it is smoothed by convoluting it with the bell-shaped bump
    function ``f(r) = max(1 - (2*r / width)**2, 0)**2``.  The bump width is
    determined by the ``relwidth`` parameter.

    This routine samples the smoothed field on a regular (square or cubic) grid
    and displays it using matplotlib's imshow.

    This function is similar to `~kwant.plotter.map`, but generally gives more
    appealing visual results when used on systems with many sites. If you want
    site-level resolution you may be better off using `~kwant.plotter.map`.

    This is a convenience function that is equivalent to
    ``scalarplot(*interpolate_density(syst, density, relwidth), **kwargs)``.
    The longer form makes it possible to tweak additional options of
    `~kwant.plotter.interpolate_density`.

    Parameters
    ----------
    syst : `kwant.system.FiniteSystem`
        The system for which to plot ``density``.
    density : sequence of float
        Sequence of values defining density on each site of the system.
        Ordered in the same way as ``syst.sites``. This typically will be
        the result of evaluating a `~kwant.operator.Density` operator.
    relwidth : float or `None`
        Relative width of the bumps used to smooth the field, as a fraction
        of the length of the longest side of the bounding box.
    **kwargs : various
        Keyword args to be passed verbatim to `~kwant.plotter.scalarplot`.

    Returns
    -------
    fig : matplotlib figure
        A figure with the output if ``ax`` is not set, else None.

    See Also
    --------
    kwant.plotter.current
    kwant.plotter.map
    """
    with _common.reraise_warnings(4):
        return scalarplot(*interpolate_density(syst, density, relwidth),
                          **kwargs)


# TODO (Anton): Fix plotting of parts of the system using color = np.nan.
# Not plotting sites currently works, not plotting hoppings does not.
# TODO (Anton): Allow a more flexible treatment of position than pos_transform
# (an interface for user-defined pos).
