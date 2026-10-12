import streamlit as st
import pandas as pd
import plotly.express as px
from streamlit_autorefresh import st_autorefresh

# Page Layout Setting
st.set_page_config(page_title="Live Airport Dashboard", layout="wide")

# Har 3 seconds baad dashboard auto-refresh hoga
st_autorefresh(interval=3000, key="airportrefresh")

st.title("🌍 Live Airport Coordinate Dashboard")
st.markdown("Yeh dashboard real-time data update ke sath airports ki locations map par dikhata hai.")

# CSV File Load karein
try:
    df = pd.read_csv("airports.csv")
except FileNotFoundError:
    st.error("airports.csv file nahi mili! Usay project folder mein rakhein.")
    st.stop()

# Sidebar Filters
st.sidebar.header("Filter Options")
selected_city = st.sidebar.selectbox("City Select Karein", ["All Cities"] + list(df["City"].unique()))

if selected_city != "All Cities":
    filtered_df = df[df["City"] == selected_city]
else:
    filtered_df = df

# Metrics Row
col1, col2 = st.columns(2)
col1.metric("Total Airports Tracked", len(df))
col2.metric("Currently Displayed", len(filtered_df))

# Interactive Map Visualization
fig = px.scatter_mapbox(
    filtered_df, 
    lat="Latitude", 
    lon="Longitude", 
    hover_name="Airport",
    hover_data=["City", "Coordinates"],
    zoom=1.5, 
    height=550
)
fig.update_layout(
    mapbox_style="carto-positron", 
    margin={"r":0,"t":0,"l":0,"b":0}
)

st.plotly_chart(fig, use_container_width=True)

# Data Table View
with st.expander("📄 Raw Data Table Dekhein"):
    st.dataframe(filtered_df, use_container_width=True)