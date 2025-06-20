#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mapa_preds.py

Aquest script llegeix un fitxer NetCDF amb prediccions meteorològiques en unitats físiques
(°C, %, mm, km/h, hPa, etc.) i genera mapes en format PNG per a diferents variables:
- Temperatura ("Temp" en °C)
- Humitat relativa ("Humitat" en %)
- Precipitació acumulada per hora ("Pluja" en mm)
- Pressió atmosfèrica ("Patm" en hPa)
- Vent ("VentFor" en km/h, més les files "Vent_u" i "Vent_v" per als components)
  
El codi utilitza interpolació lineal (amb màscara fora del convex hull) per evitar valors
extravagants. Si es desitja, també es pot triar interpolació "nearest". Per al vent, es mostra
la intensitat de la velocitat mitjançant un fons de contorns i fletxes (barbs) per als vectors.

REQUISITS:
    - Python 3.7+
    - numpy
    - scipy
    - matplotlib
    - netCDF4
    - cartopy

EXECUCIÓ: 
Aquest codi es pot executar fàcilment mitjançant l'script mapa_preds.bat, disponible en aquest repositori.

Autor: Nil Farrés Soler
"""

import os
import sys
import argparse
import numpy as np
import pandas as pd
from scipy.spatial import Delaunay
from tqdm import tqdm
import matplotlib as mpl
import matplotlib.pyplot as plt
from scipy.interpolate import griddata
from netCDF4 import Dataset, num2date
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.colors as mcolors
from scipy.spatial import cKDTree
from mpl_toolkits.axes_grid1 import make_axes_locatable
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
from scipy.ndimage import gaussian_filter

mpl.rcParams['figure.facecolor'] = 'white'
mpl.rcParams['axes.facecolor']   = 'white'
mpl.rcParams['savefig.facecolor'] = 'white'


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Genera un mapa meteorològic a partir d'un fitxer NetCDF de prediccions."
    )
    parser.add_argument(
        "--ncfile", "-n", type=str, required=True,
        help="Fitxer NetCDF amb les prediccions en unitats físiques."
    )
    parser.add_argument(
        "--time", "-t", type=int, default=0,
        help="Índex temporal (timestep) a visualitzar (valor per defecte: 0)."
    )
    parser.add_argument(
        "--variable", "-v", type=str, default="Temp",
        choices=["Temp", "Humitat", "Pluja", "Patm", "VentFor", "Vent_u", "Vent_v", "Vent"],
        help="Variable a plotejar: Temp, Humitat, Pluja, Patm, VentFor, Vent_u, Vent_v o Vent (combinat)."
    )
    parser.add_argument(
        "--interp", type=str, default="hybrid",
        choices=["linear", "nearest", "hybrid", "idw", "none"],
        help=(
        "Mètode d'interpolació:\n"
        "  'linear': interpolació lineal dins del convex hull, fora = NaN.\n"
        "  'nearest': valor del punt més proper per a tota la regió.\n"
        "  'hybrid': primer 'linear', i per a NaN (fora del hull) omple amb 'nearest'.\n"
        "  'idw': inverse distance weighting.\n"
        "  'none': només es mostren els nodes, sense interpolació (recomanat)."
        )
    )
    parser.add_argument(
        "--resol", "-r", type=int, default=300,
        help="Resolució de sortida en DPI (dots per inch) (per defecte: 300)."
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Fitxer de sortida PNG. Si no es defineix, el nom es genera automàticament."
    )
    parser.add_argument(
        "--maxdist", type=float, default=50,
        help="Distància màxima (en km) al node més proper per a interpolació (per defecte: 50)."
    )
    parser.add_argument(
        "--showhull", action="store_true",
        help="Mostra la malla del convex hull (Delaunay)."
    )
    parser.add_argument(
        "--all_times", action="store_true",
        help="Genera un mapa per a cada timestep del fitxer NetCDF."
    )

    return parser.parse_args()


def llegeix_dades(ncfile, timestep, variable):
    """
    Llegeix les dades necessàries del NetCDF:
    - Coordenades lon/lat dels nodes.
    - Prediccions per a la variable triada (o conjunts per a vent).
    - Llista de noms de variables i timestamps (opcional).
    
    Retorna:
        lon_nodes (ndarray[N]), lat_nodes (ndarray[N]),
        vals (ndarray[N]) o (vals_u, vals_v) per a vent,
        t_label (str) amb data/hora si existeix.
    """
    if not os.path.isfile(ncfile):
        raise FileNotFoundError(f"El fitxer '{ncfile}' no existeix.")

    ds = Dataset(ncfile, mode="r")

    # Assumim que hi ha variables lon_nodes i lat_nodes
    lons = ds.variables["lon"][:]   # [N,]
    lats = ds.variables["lat"][:]   # [N,]

    # Llista amb els noms de cada variable dins de ds.variables["variable"]
    # Comprovem si cada element és bytes o str, i fem el decode en cas necessari.
    raw_vars = ds.variables["variable"][:]
    var_list = []
    for v in raw_vars:
        if isinstance(v, (bytes, bytearray)):
            name = v.decode("utf-8").strip()
        else:
            # ja és str (p. ex. numpy.str_)
            name = v.strip()
        var_list.append(name)

    # Llegim timestamps (si existeix): per fer un títol més descriptiu
    t_label = None
    if "time_str" in ds.variables:
        time_str_var = ds.variables["time_str"][:]
        # Pot ser que time_str_var elements siguin bytes o ja siguin str.
        raw_t = time_str_var[timestep]
        if isinstance(raw_t, (bytes, bytearray)):
            try:
                t_label = raw_t.decode("utf-8")
            except Exception:
                t_label = f"Index {timestep}"
        else:
            # ja és str (p. ex. numpy.str_)
            t_label = raw_t

    # Llegim dades de predicció: suposem shape [T, N, n_vars]
    pred = ds.variables["prediction"][:]   # (temps, nodes, n_vars)
    if timestep < 0 or timestep >= pred.shape[0]:
        raise IndexError(f"El valor de --time ({timestep}) no és vàlid; màxim index és {pred.shape[0]-1}.")

    # Si volem "Vent" combinat, retornem components u i v
    if variable == "Vent":
        idx_u = var_list.index("Vent_u")
        idx_v = var_list.index("Vent_v")
        vals_u = pred[timestep, :, idx_u]
        vals_v = pred[timestep, :, idx_v]
        ds.close()
        return lons, lats, (vals_u, vals_v), t_label

    # Si volem VentFor, Vent_u, o Vent_v per separat
    if variable in ["VentFor", "Vent_u", "Vent_v"]:
        idx = var_list.index(variable)
        vals = pred[timestep, :, idx]
        ds.close()
        return lons, lats, vals, t_label

    # Altres variables scalars (Temp, Humitat, Pluja, Patm)
    idx = var_list.index(variable)
    vals = pred[timestep, :, idx]
    ds.close()
    return lons, lats, vals, t_label


def crea_graella(lons, lats, margin=0.1, resolucion=200):
    """
    Genera una graella regular (xi, yi) per a la interpolació.
    - margin: marge (en graus) al voltant de min/max de lon/lat.
    - resolucion: nombre de punts per eix (ex. 200x200).
    
    Retorna:
        xi, yi (meshgrid) i llistes 1D xi_lin, yi_lin.
    """
    lon_min = lons.min() - margin
    lon_max = lons.max() + margin
    lat_min = lats.min() - margin
    lat_max = lats.max() + margin

    xi_lin = np.linspace(lon_min, lon_max, resolucion)
    yi_lin = np.linspace(lat_min, lat_max, resolucion)
    xi, yi = np.meshgrid(xi_lin, yi_lin)
    return xi, yi, xi_lin, yi_lin


def interpolar_valors(lons, lats, vals, xi, yi, metode="linear", max_dist_km=None):
    """
    Interpola els valors scalars (vals) donats en punts no regulars (lons, lats)
    cap a la graella regular (xi, yi).
    - metode: 'linear', 'nearest', 'hybrid'
    - max_dist_km: si s'especifica, màscara els punts de la graella a més distància del node més proper.
    Retorna:
        zi: matriu 2D amb valors interpolats (i NaN fora del convex hull en cas de linear o si es supera max_dist_km).
    """
    # 1) Filtrar tots els punts on vals sigui NaN
    mask_valid = ~np.isnan(vals)
    points = np.vstack((lons[mask_valid], lats[mask_valid])).T
    vals_valid = vals[mask_valid]

    # 2) Interpolació
    if metode == "idw":
        # 1) Fem la interpolació IDW (ara amb power=1 per defecte)
        zi_idw = idw_interpolation(lons[mask_valid], lats[mask_valid], vals_valid, xi, yi)
        # 2) Apliquem un suau filtre gaussià per eliminar els "bullseyes"
        #    Sigma petit: 0.8 aprox., depèn de quant vols suavitzar
        zi = gaussian_filter(zi_idw, sigma=0.8)
    elif metode == "nearest":
        zi = griddata(points, vals_valid, (xi, yi), method="nearest")
    elif metode == "linear":
        zi = griddata(points, vals_valid, (xi, yi), method="linear")
    elif metode == "hybrid":
        zi_linear = griddata(points, vals_valid, (xi, yi), method="linear")
        zi_nearest = griddata(points, vals_valid, (xi, yi), method="nearest")
        zi = np.where(np.isnan(zi_linear), zi_nearest, zi_linear)
    else:
        raise ValueError(f"Mètode d'interpolació incorrecte: {metode}")

    # 3) Si es vol màscarar per distància màxima (opcional)
    if max_dist_km is not None:
        # Convertim lat/lon a km aproximadament (ignorem curvatura, bona aproximació a escala regional)
        def haversine(lon1, lat1, lon2, lat2):
            # Radi de la Terra (km)
            R = 6371.0
            lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])
            dlon = lon2 - lon1
            dlat = lat2 - lat1
            a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
            c = 2 * np.arcsin(np.sqrt(a))
            return R * c

        # Flatten graella
        xi_flat = xi.flatten()
        yi_flat = yi.flatten()
        # Calculem distància a cada node (més ràpid amb KDTree)
        tree = cKDTree(points)
        # Obtenim distància en graus i índex del node més proper per a cada punt de la graella
        dists_grad, idxs = tree.query(np.vstack((xi_flat, yi_flat)).T, k=1)
        # Extraiem lon/lat dels nodes més propers
        lons_nodes_prop = points[idxs, 0]
        lats_nodes_prop = points[idxs, 1]
        # Haversine entre cada punt de la graella (lon/lat) i el seu node més proper
        def haversine_vec(lon1, lat1, lon2, lat2):
            R = 6371.0
            lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])
            dlon = lon2 - lon1
            dlat = lat2 - lat1
            a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
            c = 2 * np.arcsin(np.sqrt(a))
            return R * c
        dists_km = haversine_vec(xi_flat, yi_flat, lons_nodes_prop, lats_nodes_prop)
        mask = dists_km.reshape(xi.shape) > max_dist_km
        zi[mask] = np.nan

    return zi

def idw_interpolation(lons, lats, vals, xi, yi, power=1):
    points = np.vstack((lons, lats)).T
    grid_points = np.vstack((xi.flatten(), yi.flatten())).T
    dists = np.linalg.norm(points[None, :, :] - grid_points[:, None, :], axis=2)
    weights = 1.0 / (dists ** power + 1e-12)
    weights /= weights.sum(axis=1, keepdims=True)
    zi = np.dot(weights, vals)
    return zi.reshape(xi.shape)

def plota_scalar(xi, yi, zi, lons, lats, vals, variable, t_label, output_file,
                 cmap="plasma", levels=100, resol_dpi=300, masquejar_exterior=True,
                 vmin=None, vmax=None, args=None):
    """
    Plota un mapa per a una variable scalar:
    - xi, yi: amb lon/lat (2D).
    - zi: valors interpolats (2D), pot contenir NaN.
    - lons, lats, vals: coordenades i valors reals per a dibuixar punts.
    - variable: nom de la variable (per títol, colorbar).
    - t_label: descripció temporal per al títol.
    - output_file: nom de l'arxiu PNG per desar.
    - cmap: colormap de matplotlib.
    - levels: nombre de nivells per a contourf.
    - resol_dpi: DPI de la imatge final.
    - masquejar_exterior: si True, no pinta zones on zi és NaN.
    """

    if zi is None:
        # Mode sense interpolació: només scatter dels nodes (recomanat)
        fig = plt.figure(figsize=(10, 8), dpi=resol_dpi, facecolor='white')
        ax = plt.axes(projection=ccrs.PlateCarree(), facecolor='white')
        ax.add_feature(cfeature.LAND.with_scale("10m"), facecolor="white", zorder=0)
        ax.add_feature(cfeature.OCEAN.with_scale("10m"), facecolor="white", zorder=0)
        ax.add_feature(cfeature.COASTLINE.with_scale("10m"), linewidth=0.8, edgecolor="black")
        ax.add_feature(cfeature.BORDERS.with_scale("10m"), linewidth=0.6, edgecolor="gray")
        ax.add_feature(cfeature.RIVERS.with_scale("10m"), linewidth=0.5, edgecolor="blue", alpha=0.5)
        ax.add_feature(cfeature.LAKES.with_scale("10m"), facecolor="lightblue", alpha=0.5)
        ax.add_feature(cfeature.STATES.with_scale("10m"), linewidth=0.5, edgecolor="gray", linestyle=":")
        # Extensió del mapa segons nodes
        margin = 0.1
        ax.set_extent([
            lons.min() - margin, lons.max() + margin,
            lats.min() - margin, lats.max() + margin
        ], crs=ccrs.PlateCarree())

        # FILTRA NOMÉS ELS VALORS NO NaN!
        mask_valid = ~np.isnan(vals)
        lons_valid = lons[mask_valid]
        lats_valid = lats[mask_valid]
        vals_valid = vals[mask_valid]

        sc = ax.scatter(
            lons_valid, lats_valid, c=vals_valid,
            cmap=cmap, edgecolor="k", linewidth=0.4, s=50, alpha=0.9,
            transform=ccrs.PlateCarree(), zorder=3,
            vmin=vmin, vmax=vmax
        )
        cbar = plt.colorbar(sc, ax=ax, orientation="vertical", pad=0.02, shrink=0.8)
        units = {
            "Temp": "°C", "Humitat": "%", "Pluja": "mm", "Patm": "hPa", "VentFor": "km/h",
            "Vent_u": "km/h", "Vent_v": "km/h"
        }
        cbar.set_label(f"{variable} ({units.get(variable, '')})", fontsize=12)
        cbar.ax.tick_params(labelsize=10)
        titles = {
            "Temp": "Temperatura a 2m",
            "Humitat": "Humitat relativa a 2m",
            "Pluja": "Pluja acumulada",
            "Patm": "Pressió atmosfèrica",
            "Vent": "Vent a 10m",
            "VentFor": "Velocitat del vent a 10m",
            "Vent_u": "Component U del vent a 10m",
            "Vent_v": "Component V del vent a 10m"
        }
        title = f"{titles.get(variable, variable)} : {t_label}" if t_label else f"{titles.get(variable, variable)}"
        ax.set_title(f"{title}", fontsize=14, fontweight='bold', pad=20)
        ax.set_xlabel("Longitude (°)", fontsize=12)
        ax.set_ylabel("Latitude (°)", fontsize=12)
        ax.tick_params(labelsize=10)
        min_val = np.nanmin(vals_valid)
        max_val = np.nanmax(vals_valid)
        unitat = units.get(variable, "")
        plt.subplots_adjust()
        ax.text(
            0.99, 0.01,
            f"Min: {min_val:.2f} {unitat} · Max: {max_val:.2f} {unitat}\nMeteoGraphPC · TFG Nil Farrés",
            transform=ax.transAxes,
            ha="right", va="bottom", fontsize=9, color="gray",
            bbox=dict(facecolor="white", alpha=0.3, edgecolor="none", pad=1)
        )
        fig.savefig(output_file, dpi=resol_dpi)
        plt.close(fig)
        return

    # 1) Creem la figura i l'eix amb projecció PlateCarree i forcem fons blanc
    fig = plt.figure(figsize=(10, 8), dpi=resol_dpi, facecolor='white')
    ax = plt.axes(projection=ccrs.PlateCarree(), facecolor='white')

    # 2) Afegim les capes de terra i oceà amb facecolor blanc
    ax.add_feature(cfeature.LAND.with_scale("10m"), facecolor="white", zorder=0)
    ax.add_feature(cfeature.OCEAN.with_scale("10m"), facecolor="white", zorder=0)
    # (Coastline i Borders ja es dibuixen sobre aquest fons)

    # 2) Configurem els límits del mapa segons xi i yi
    margin = 0.1
    ax.set_extent([
        xi.min() - margin, xi.max() + margin,
        yi.min() - margin, yi.max() + margin
    ], crs=ccrs.PlateCarree())

    # 3) Afegim característiques geogràfiques:
    ax.add_feature(cfeature.COASTLINE.with_scale("10m"), linewidth=0.8, edgecolor="black")
    ax.add_feature(cfeature.BORDERS.with_scale("10m"), linewidth=0.6, edgecolor="gray")
    ax.add_feature(cfeature.RIVERS.with_scale("10m"), linewidth=0.5, edgecolor="blue", alpha=0.5)
    ax.add_feature(cfeature.LAKES.with_scale("10m"), facecolor="lightblue", alpha=0.5)
    ax.add_feature(cfeature.STATES.with_scale("10m"), linewidth=0.5, edgecolor="gray", linestyle=":")

    # 5) Dibuixem la superfície interpolada amb contourf
    cf = ax.contourf(
        xi, yi, np.ma.masked_where(np.isnan(zi), zi),
        levels=levels,
        cmap=cmap,
        transform=ccrs.PlateCarree(),
        zorder=2,
        antialiased=True,
        vmin=vmin, vmax=vmax  # <-- AFEGEIX AIXÒ!
    )

    if variable == "Temp":
        ax.contour(xi, yi, zi, levels=[0], colors="k", linewidths=1.2, linestyles="--", transform=ccrs.PlateCarree(), zorder=4)
    if variable == "Patm":
        ax.contour(xi, yi, zi, levels=[1013], colors="k", linewidths=1.2, linestyles="--", transform=ccrs.PlateCarree(), zorder=4)
    if variable == "Pluja":
        ax.contour(xi, yi, zi, levels=[20, 50], colors="k", linewidths=1, linestyles="--", transform=ccrs.PlateCarree(), zorder=4)
    if variable == "VentFor":
        ax.contour(xi, yi, zi, levels=np.arange(0, 100, 10), colors="k", linewidths=0.8, linestyles="--", transform=ccrs.PlateCarree(), zorder=4)
    if variable in ["Vent_u", "Vent_v"]:
        ax.contour(xi, yi, zi, levels=np.arange(-50, 51, 10), colors="k", linewidths=0.8, linestyles="--", transform=ccrs.PlateCarree(), zorder=4)
    if variable == "Vent":
        # Per al vent, dibuixem les barbs
        scale_factor = 0.05
        u = np.cos(np.radians(zi)) * scale_factor
        v = np.sin(np.radians(zi)) * scale_factor
        ax.quiver(
            xi, yi, u, v,
            angles="xy", scale_units="xy", scale=1,
            color="blue", alpha=0.7, zorder=3,
            transform=ccrs.PlateCarree()
        )
    else:
        # Per altres variables, només contourf
        pass

    # 6) Reforcem els límits del convex hull (opcional):
    #    Dibuixem la triangulació de Delaunay per veure'n l'estructura
    if getattr(args, 'showhull', False):
        try:
            from scipy.spatial import Delaunay
            points = np.vstack((lons, lats)).T
            tri = Delaunay(points)
            ax.triplot(
                points[:, 0], points[:, 1], tri.simplices,
                color="gray", lw=0.5, linestyle="--", alpha=0.4, transform=ccrs.PlateCarree(), zorder=2
            )
        except Exception:
            pass

    mask_pts = ~np.isnan(vals)
    lons_pts = lons[mask_pts]
    lats_pts = lats[mask_pts]
    vals_pts = vals[mask_pts]

    # Màscara per als zeros exactes
    mask_zero = (vals_pts == 0)
    mask_nozero = (vals_pts != 0)

    # Primer pinta els zeros de blanc
    ax.scatter(
        lons_pts[mask_zero], lats_pts[mask_zero], c="#FFFFFF",
        edgecolor="k", linewidth=0.4, s=25, alpha=0.9,
        transform=ccrs.PlateCarree(), zorder=3
    )
    # Ara pinta la resta de punts (>0) amb el colormap normal
    sc = ax.scatter(
        lons_pts[mask_nozero], lats_pts[mask_nozero], c=vals_pts[mask_nozero],
        cmap=cmap, edgecolor="k", linewidth=0.4, s=25, alpha=0.9,
        transform=ccrs.PlateCarree(), zorder=4, vmin=vmin, vmax=vmax
    )

    # 8) Afegim colorbar
    cbar = plt.colorbar(cf, ax=ax, orientation="vertical", pad=0.02, shrink=0.8)

    units = {
    "Temp": "°C",
    "Humitat": "%",
    "Pluja": "mm",
    "Patm": "hPa",
    "VentFor": "km/h",
    "Vent_u": "km/h",
    "Vent_v": "km/h"
    }
    cbar.set_label(f"{variable} ({units.get(variable, '')})", fontsize=12)

    cbar.ax.tick_params(labelsize=10)

    # Afegeix mínim i màxim sota la colorbar
    min_val = np.nanmin(vals)
    max_val = np.nanmax(vals)
    unitat = units.get(variable, "")


    # 9) Títols i eixos
    titles = {
        "Temp": "Temperatura a 2m",
        "Humitat": "Humitat relativa a 2m",
        "Pluja": "Pluja acumulada",
        "Patm": "Pressió atmosfèrica",
        "Vent": "Vent a 10m",
        "VentFor": "Velocitat del vent a 10m",
        "Vent_u": "Component U del vent a 10m",
        "Vent_v": "Component V del vent a 10m"
    }
    title = f"{titles.get(variable, variable)} : {t_label}" if t_label else f"{titles.get(variable, variable)}"

    ax.set_title(f"{title}", fontsize=12, fontweight='bold', pad=20)
    ax.set_xlabel("Longitude (°)", fontsize=12)
    ax.set_ylabel("Latitude (°)", fontsize=12)
    ax.tick_params(labelsize=10)

    # 10) Desem la figura
    plt.subplots_adjust()
    ax.text(
        0.99, 0.01,
        f"Min: {min_val:.2f} {unitat} · Max: {max_val:.2f} {unitat}\nMeteoGraphPC · TFG Nil Farrés",
        transform=ax.transAxes,
        ha="right", va="bottom", fontsize=9, color="gray",
        bbox=dict(facecolor="white", alpha=0.3, edgecolor="none", pad=1)
    )
    fig.savefig(output_file, dpi=resol_dpi)
    plt.close(fig)


def plota_vent(lons, lats, vals_u, vals_v, variable, t_label, output_file, resol_dpi=300, max_dist_km=50):
    """
    Dibuixa un mapa de vent combinat, ara amb màscara de distància màxima com la resta de variables.
    """
    speed = np.sqrt(vals_u**2 + vals_v**2)
    mask_valid = ~np.isnan(speed)
    xi, yi, _, _ = crea_graella(lons, lats, margin=0.1, resolucion=200)

    # Ara: interpolació IDW + suavitzat (en comptes de lineal)
    zi_idw = idw_interpolation(lons[mask_valid], lats[mask_valid], speed[mask_valid], xi, yi)
    zi = gaussian_filter(zi_idw, sigma=0.8)
    # Mascarem segons la distància màxima (igual que interpolar_valors)
    # 1) Nodes reals vàlids:
    points = np.vstack((lons[mask_valid], lats[mask_valid])).T
    from scipy.spatial import cKDTree
    def haversine(lon1, lat1, lon2, lat2):
        R = 6371.0
        lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
        c = 2 * np.arcsin(np.sqrt(a))
        return R * c
    xi_flat = xi.flatten()
    yi_flat = yi.flatten()
    tree = cKDTree(points)
    dists_grad, idxs = tree.query(np.vstack((xi_flat, yi_flat)).T, k=1)
    lons_nodes_prop = points[idxs, 0]
    lats_nodes_prop = points[idxs, 1]
    # Haversine vectoritzat:
    def haversine_vec(lon1, lat1, lon2, lat2):
        R = 6371.0
        lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
        c = 2 * np.arcsin(np.sqrt(a))
        return R * c

    dists_km = haversine_vec(xi_flat, yi_flat, lons_nodes_prop, lats_nodes_prop)
    mask = dists_km.reshape(xi.shape) > max_dist_km
    zi[mask] = np.nan

    vmin, vmax = np.nanmin(speed), np.nanmax(speed)
    zi = np.clip(zi, vmin, vmax)

    fig = plt.figure(figsize=(10, 8), dpi=resol_dpi)
    ax = plt.axes(projection=ccrs.PlateCarree())
    margin = 0.1
    ax.set_extent([
        xi.min() - margin, xi.max() + margin,
        yi.min() - margin, yi.max() + margin
    ], crs=ccrs.PlateCarree())

    ax.add_feature(cfeature.COASTLINE.with_scale("10m"), linewidth=0.8, edgecolor="black")
    ax.add_feature(cfeature.BORDERS.with_scale("10m"), linewidth=0.6, edgecolor="gray")
    ax.add_feature(cfeature.STATES.with_scale("10m"), linewidth=0.5, edgecolor="gray", linestyle=":")
    ax.add_feature(cfeature.RIVERS.with_scale("10m"), linewidth=0.5, edgecolor="blue", alpha=0.5)
    ax.add_feature(cfeature.LAKES.with_scale("10m"), facecolor="lightblue", alpha=0.5)

    cf = ax.contourf(
        xi, yi, zi,
        levels=100,
        cmap="viridis",
        transform=ccrs.PlateCarree(),
        zorder=1,
        antialiased=True
    )
    skip = max(1, int(len(lons) / 200))
    ax.barbs(
        lons[::skip], lats[::skip],
        vals_u[::skip], vals_v[::skip],
        length=6, pivot="middle", linewidth=0.6,
        color="black", transform=ccrs.PlateCarree(),
        zorder=2
    )
    cbar = plt.colorbar(cf, ax=ax, orientation="vertical", pad=0.02, shrink=0.8)
    cbar.set_label("Velocitat del vent (km/h)", fontsize=12)
    cbar.ax.tick_params(labelsize=10)

    min_val = np.nanmin(speed)
    max_val = np.nanmax(speed)

    title = f"Vent a 10m : {t_label}" if t_label else "Vent 10m"
    ax.set_title(f"{title}", fontsize=16, fontweight='bold', pad=20)
    ax.set_xlabel("Longitude (°)", fontsize=12)
    ax.set_ylabel("Latitude (°)", fontsize=12)
    ax.tick_params(labelsize=10)
    plt.subplots_adjust()
    ax.text(
        0.99, 0.01,
        f"Min: {min_val:.2f} · Max: {max_val:.2f} \nMeteoGraphPC · TFG Nil Farrés",
        transform=ax.transAxes,
        ha="right", va="bottom", fontsize=9, color="gray",
        bbox=dict(facecolor="white", alpha=0.3, edgecolor="none", pad=1)
    )
    fig.savefig(output_file, dpi=resol_dpi)
    plt.close(fig)


def filtra_duplicats(lons, lats, *vals_arrays):
    """
    Filtra duplicats de lat/lon, conservant només el primer.
    Retorna els arrays lons, lats, [vals_arrays...] sense duplicats.
    """
    df = pd.DataFrame({'lon': lons, 'lat': lats})
    for i, arr in enumerate(vals_arrays):
        df[f'val_{i}'] = arr
    df_filtrat = df.drop_duplicates(['lat', 'lon'], keep='first')
    arrays = [np.array(df_filtrat['lon']), np.array(df_filtrat['lat'])]
    for i in range(len(vals_arrays)):
        arrays.append(np.array(df_filtrat[f'val_{i}']))
    return tuple(arrays)


# Temperatura
cmap_temp = mcolors.LinearSegmentedColormap.from_list(
    "MeteoTemp",
    [
        (0.00, "#0033A0"), # blau fosc (molt fred)
        (0.22, "#339FFF"), # blau clar (fred)
        (0.40, "#A7F797"), # verd clar (temperatura suau, ~15-18°C)
        (0.55, "#F7F797"), # groc pàl·lid (suau/càlid, ~22-25°C)
        (0.70, "#FF8C00"), # taronja (càlid, ~30°C)
        (0.88, "#FF1E00"), # vermell (molt càlid)
        (1.00, "#A70000"), # granate (extrem)
    ]
)

# Pluja
cmap_pluja = mcolors.LinearSegmentedColormap.from_list(
    "MeteoPluja",
    [
        (0.00, "#FFFFFF"),   # BLANC pels zeros exactes!
        (0.01, "#B2D2F1"),  # Ja comença el blau molt clar
        (0.15, "#88BFFD"),
        (0.20, "#5C9BE7"),
        (0.30, "#2A94EB"),
        (0.40, "#007CC4"),
        (0.60, "#004A8B"),
        (0.85, "#002050"),
        (1.00, "#4B0469"),
    ]
)

# Humitat
cmap_humitat = mcolors.LinearSegmentedColormap.from_list(
    "MeteoHumitat",
    [
        (0.00, "#F7FCF5"),  # blanc-groc verdós (0%)
        (0.20, "#D9F0A3"),  # verd molt clar (~20%)
        (0.40, "#A1D99B"),  # verd clar (~40%)
        (0.65, "#41AB5D"),  # verd viu (~65%)
        (0.85, "#238B45"),  # verd fosc (~85%)
        (1.00, "#00441B"),  # verd molt fosc (100%)
    ]
)

# Pressió atmosfèrica
cmap_patm = mcolors.LinearSegmentedColormap.from_list(
    "MeteoPatm",
    [
        (0.00, "#4A90E2"),   # blau (baixa pressió)
        (0.40, "#A7F797"),   # verd (1013 hPa)
        (1.00, "#BD10E0"),   # porpra (alta pressió)
    ]
)

# Velocitat del vent
cmap_ventfor = mcolors.LinearSegmentedColormap.from_list(
    "MeteoVentFor",
    [
        (0.00, "#C7E6FF"),  # blau cel
        (0.08, "#A1D0FF"),  # blau molt clar
        (0.16, "#75C0FF"),  # blau clar
        (0.22, "#5BA3F7"),  # blau mitjà
        (0.28, "#5180CF"),  # blau intens
        (0.34, "#6E6FC9"),  # blau-lila suau
        (0.40, "#8B77D1"),  # blau-lila
        (0.48, "#A086E2"),  # lila-blau clar
        (0.56, "#B699F4"),  # lila clar
        (0.62, "#A984E8"),  # lila clàssic
        (0.70, "#9C6BC8"),  # lila mitjà
        (0.78, "#8B49C6"),  # lila/morat
        (0.86, "#713BAF"),  # morat intens
        (0.93, "#613DC1"),  # morat fosc
        (1.00, "#2B184E"),  # morat-negre
    ]
)

def main():
    args = parse_arguments()

    # Obrim el fitxer NetCDF només per saber quants timesteps hi ha
    ds = Dataset(args.ncfile, mode="r")
    num_times = ds.variables["prediction"].shape[0]
    ds.close()

    # Si --all_times, fem servir tqdm
    times_iter = [args.time]
    if getattr(args, 'all_times', False):
        times_iter = range(num_times)
        print(f"Generant mapes per a {num_times} timesteps...")

    for t in tqdm(times_iter, desc="Generant mapes"):
        try:
            data = llegeix_dades(args.ncfile, t, args.variable)
        except Exception as e:
            print(f"Error llegint dades per timestep {t}: {e}", file=sys.stderr)
            continue

        lons, lats = data[0], data[1]
        if args.variable == "Vent":
            vals_u, vals_v = data[2]
            t_label = data[3]
            lons, lats, vals_u, vals_v = filtra_duplicats(lons, lats, vals_u, vals_v)
        else:
            vals = data[2]
            t_label = data[3]
            lons, lats, vals = filtra_duplicats(lons, lats, vals)

        # Nom de sortida
        if args.output:
            base, ext = os.path.splitext(args.output)
            output_file = f"{base}_t{t}{ext}"
        else:
            t_str = t_label.replace(" ", "_").replace(":", "-") if t_label else f"t{t}"
            output_file = f"mapa_{args.variable}_{t_str}.png"

        if args.variable == "Vent":
            plota_vent(
                lons, lats, vals_u, vals_v,
                variable="Vent", t_label=t_label,
                output_file=output_file,
                resol_dpi=args.resol,
                max_dist_km=args.maxdist
            )
        else:
            if args.interp == "none":
                # Només es mostren els nodes
                xi = yi = zi = None
            else:
                xi, yi, _, _ = crea_graella(lons, lats, margin=0.1, resolucion=args.resol)
                zi = interpolar_valors(lons, lats, vals, xi, yi, metode=args.interp, max_dist_km=args.maxdist)


            if args.variable == "Temp":
                cmap_sel = cmap_temp
                vmin, vmax = -20, 45
            elif args.variable == "Humitat":
                cmap_sel = cmap_humitat
                vmin, vmax = 0, 100
            elif args.variable == "Pluja":
                cmap_sel =  cmap_pluja
                vmin, vmax = 0, 150
            elif args.variable == "Patm":
                cmap_sel = cmap_patm
                vmin, vmax = 980, 1040
            elif args.variable == "VentFor":
                cmap_sel = cmap_ventfor
                vmin, vmax = 0, 200
            else:
                cmap_sel = "viridis"
                vmin, vmax = None, None

            plota_scalar(
                xi, yi, zi,
                lons, lats, vals,
                variable=args.variable,
                t_label=t_label,
                output_file=output_file,
                cmap=cmap_sel,
                levels=100, resol_dpi=args.resol,
                masquejar_exterior=(args.interp == "linear"),
                vmin=vmin, vmax=vmax,
                args=args
            )
        print(f"\nMapa de {args.variable} t={t} desat a: {output_file}")


if __name__ == "__main__":
    main()
