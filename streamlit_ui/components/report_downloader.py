"""
Streamlit Report Downloader Component

Handles downloading report files from the virtual filesystem.
Provides download button and report listing functionality.
"""

import streamlit as st
from typing import List, Optional


def render_report_downloader(service) -> None:
    """
    Render report download interface in sidebar.
    
    Shows list of available reports for current session
    and provides download buttons.
    
    Args:
        service: PenTestAgentService instance
    """
    st.sidebar.markdown("### 📥 Download Reports")
    
    import asyncio
    reports = asyncio.run(service.list_reports())
    
    if not reports:
        st.sidebar.info("No reports generated yet.")
        return
        
    st.sidebar.write(f"**{len(reports)}** reports available:")
    
    for report_name in reports:
        col1, col2 = st.sidebar.columns([3, 1])
        
        with col1:
            st.text(f"📄 {report_name}")
            
        with col2:
            # Create download button
            report_content = asyncio.run(service.download_report(report_name))
            
            if report_content:
                st.download_button(
                    label="⬇️",
                    data=report_content,
                    file_name=report_name,
                    mime="text/markdown",
                    key=f"download_{report_name}"
                )
                

def download_single_report(service, filename: str) -> Optional[str]:
    """
    Download a single report file.
    
    Args:
        service: PenTestAgentService instance
        filename: Name of report to download
        
    Returns:
        Report content if found, None otherwise
    """
    import asyncio
    return asyncio.run(service.download_report(filename))


def display_report_preview(service, filename: str) -> Optional[str]:
    """
    Display preview of a report in the main area.
    
    Args:
        service: PenTestAgentService instance
        filename: Name of report to preview
        
    Returns:
        Report content if found, None otherwise
    """
    import asyncio
    content = asyncio.run(service.download_report(filename))
    
    if content:
        st.expander(f"📄 Preview: {filename}").markdown(content)
        return content
    
    return None
