# financial_dashboard.py

# --- Imports and Setup ---
import streamlit as st
import pandas as pd
import io
import plotly.express as px
import openai
import os
import json
from dotenv import load_dotenv
from datetime import datetime
import pytz # For timezone - pip install pytz

# --- Load Environment Variables ---
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# --- Page Configuration ---
st.set_page_config(
    page_title="Financial Insights Dashboard",
    layout="wide",
    initial_sidebar_state="expanded" # Keep sidebar open initially
)

# --- Account Mapping (Verify these match your L1 Names) ---
INCOME_ACCOUNTS_L1 = ['Direct Incomes', 'Indirect Incomes', 'Sales Accounts']
EXPENSE_ACCOUNTS_L1 = ['Direct Expenses', 'Indirect Expenses', 'Purchase Accounts']
ASSET_ACCOUNTS_L1 = ['Current Assets', 'Fixed Assets']
LIABILITY_ACCOUNTS_L1 = ['Current Liabilities', 'Loans (Liability)']
EQUITY_ACCOUNTS_L1 = ['Capital Account']

# --- INR Formatting Function ---
def format_inr(value):
    """Formats a number into Indian Currency style (₹ xx,xx,xxx.xx)."""
    if not isinstance(value, (int, float)):
        return value # Return as is if not a number
    try:
        # Handle potential NaN or infinite values gracefully
        if not pd.notnull(value) or not pd.api.types.is_finite(value):
            return "N/A" # Or return an empty string, or 0 formatted

        # Use standard formatting first to handle decimals and commas correctly
        value_str = f"{abs(value):,.2f}"
        # Split integer and decimal parts
        parts = value_str.split('.')
        integer_part_std = parts[0]
        decimal_part = parts[1] if len(parts) > 1 else "00"

        # Remove standard commas for reprocessing
        integer_part = integer_part_std.replace(',', '')

        # Apply Indian numbering system logic
        l = len(integer_part)
        if l <= 3:
            formatted_int = integer_part
        else:
            last_three = integer_part[-3:]
            other_digits = integer_part[:-3]
            formatted_int = ""
            for i in range(len(other_digits)):
                formatted_int += other_digits[i]
                if (len(other_digits) - 1 - i) % 2 == 0 and i != len(other_digits) - 1:
                    formatted_int += ","
            formatted_int += ',' + last_three

        sign = "-" if value < 0 else ""
        return f"₹ {sign}{formatted_int}.{decimal_part}"
    except Exception: # Catch potential errors during complex formatting
        # Fallback to a simpler representation if custom formatting fails
        try:
            return f"₹ {value:,.2f}"
        except: # Ultimate fallback
             return str(value) # Return original value as string if all else fails

# --- Helper Function to load and process data ---
def load_data(uploaded_file):
    """Loads data from the uploaded Excel file."""
    if uploaded_file is not None:
        try:
            excel_data = pd.ExcelFile(uploaded_file)
            df = pd.read_excel(excel_data, sheet_name=0) # Assume data is on the first sheet

            # --- Basic Data Cleaning ---
            num_cols = ['DebitAmt', 'CreditAmt', 'Net Effect']
            for col in num_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                else:
                    st.error(f"Error: Column '{col}' not found in the uploaded file.")
                    return None

            if 'GroupName' in df.columns:
                 df['GroupName'] = df['GroupName'].fillna('N/A')
            else:
                 st.warning("Warning: 'GroupName' column not found. Hierarchy may be incomplete.")

            if 'BaseType' in df.columns:
                df['BaseType'] = pd.to_numeric(df['BaseType'], errors='coerce').fillna(0).astype(int)
            else:
                 st.error("Error: 'BaseType' column not found.")
                 return None

            if 'Name' not in df.columns:
                st.error("Error: Column 'Name' not found.")
                return None

            # Add a column for absolute net effect for charting positive values
            df['Abs Net Effect'] = df['Net Effect'].abs()
            # Add a column for reporting value (Assets/Expenses opposite sign of Net Effect)
            # Adjusted logic for Reporting Value based on group name for levels > 1
            # This needs careful verification against accounting rules
            def calculate_reporting_value(row):
                is_asset_or_expense_l1 = row['BaseType'] == 1 and (row['Name'] in ASSET_ACCOUNTS_L1 or row['Name'] in EXPENSE_ACCOUNTS_L1)
                # Check parent group if BaseType > 1
                is_child_of_asset_or_expense = False
                if row['BaseType'] > 1:
                    # Need to trace back the ultimate L1 parent, this simple check might be insufficient for deep hierarchies
                    # Assuming GroupName directly holds an L1 name for BaseType 2 (or requires recursive lookup)
                    # Simple check for now: if parent group is asset/expense L1
                    if row['GroupName'] in ASSET_ACCOUNTS_L1 or row['GroupName'] in EXPENSE_ACCOUNTS_L1:
                         is_child_of_asset_or_expense = True # This logic might need refinement based on actual data structure/hierarchy depth

                if is_asset_or_expense_l1 or is_child_of_asset_or_expense:
                    return -row['Net Effect']
                else:
                    return row['Net Effect']

            df['Reporting Value'] = df.apply(calculate_reporting_value, axis=1)

            return df

        except Exception as e:
            st.error(f"Error loading or processing file: {e}")
            return None
    return None

# --- Function to build Hierarchical Data for Statements ---
def build_hierarchy(df, parent_name=None, level=1, target_accounts=None):
    """Recursive function to build hierarchy for statements."""
    items = []
    # Find items at the current level under the specified parent
    if level == 1:
         # Filter only the top-level accounts specified for this section
         current_level_df = df[(df['BaseType'] == level) & (df['Name'].isin(target_accounts))].copy()
    else:
         # Find items where GroupName matches the parent's Name
         current_level_df = df[(df['BaseType'] == level) & (df['GroupName'] == parent_name)].copy()

    current_level_df = current_level_df.sort_values(by='Name')

    for _, row in current_level_df.iterrows():
        item_name = row['Name']
        # Use 'Reporting Value' for display in statements
        item_value = row['Reporting Value']

        # Indentation based on level
        indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * (level - 1) # Use HTML spaces for indentation in dataframe/table
        items.append({"Name": f"{indent}{item_name}", "Value": item_value, "Level": level})

        # Recursively add children
        items.extend(build_hierarchy(df, parent_name=item_name, level=level + 1))
    return items

# --- Function to generate P&L Statement ---
def generate_pnl(df, consolidated=False):
    """Generates a formatted P&L statement with INR formatting."""
    pnl_data = []
    total_income = 0
    total_expense = 0

    # Define the L1 accounts relevant to P&L
    pnl_relevant_l1 = INCOME_ACCOUNTS_L1 + EXPENSE_ACCOUNTS_L1
    # Create a filtered dataframe containing only P&L related accounts and their children
    # This requires identifying all descendants, which can be complex.
    # Simpler approach: Filter hierarchy *after* building it for all income/expense groups.

    # --- Income ---
    all_income_items = []
    if not consolidated:
        pnl_data.append({"Name": "<b>Income</b>", "Value": "", "Level": 0, "IsHeader": True}) # Section header bolded
        for acc_l1 in INCOME_ACCOUNTS_L1:
             # Build hierarchy starting from this L1 account
             all_income_items.extend(build_hierarchy(df, level=1, target_accounts=[acc_l1]))
        pnl_data.extend(all_income_items)
    # Calculate total income from L1 accounts
    total_income = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(INCOME_ACCOUNTS_L1)), 'Reporting Value'].sum()
    pnl_data.append({"Name": "<b>Total Income</b>", "Value": total_income, "Level": 0, "IsTotal": True})

    pnl_data.append({"Name": "", "Value": "", "Level": 0}) # Spacer

    # --- Expenses ---
    all_expense_items = []
    if not consolidated:
        pnl_data.append({"Name": "<b>Expenses</b>", "Value": "", "Level": 0, "IsHeader": True}) # Section header
        for acc_l1 in EXPENSE_ACCOUNTS_L1:
            all_expense_items.extend(build_hierarchy(df, level=1, target_accounts=[acc_l1]))
        pnl_data.extend(all_expense_items)
    # Calculate total expense from L1 accounts
    total_expense = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(EXPENSE_ACCOUNTS_L1)), 'Reporting Value'].sum()
    pnl_data.append({"Name": "<b>Total Expenses</b>", "Value": total_expense, "Level": 0, "IsTotal": True})

    pnl_data.append({"Name": "", "Value": "", "Level": 0}) # Spacer

    # --- Profit/Loss ---
    net_profit_loss = total_income - total_expense
    profit_loss_label = "Net Profit/(Loss)"
    pnl_data.append({"Name": f"<b>{profit_loss_label}</b>", "Value": net_profit_loss, "Level": 0, "IsTotal": True})

    pnl_df = pd.DataFrame(pnl_data)
    # Format Value column USING format_inr
    pnl_df['Formatted Value'] = pd.to_numeric(pnl_df['Value'], errors='coerce').apply(lambda x: format_inr(x) if pd.notnull(x) else "")

    # Return the formatted DataFrame and the raw numerical profit/loss
    return pnl_df[['Name', 'Formatted Value']], net_profit_loss

# --- Function to generate Balance Sheet ---
def generate_bs(df, net_profit_loss, consolidated=False):
    """Generates a formatted Balance Sheet with INR formatting."""
    bs_data = []
    total_assets = 0
    total_liabilities = 0
    total_equity = 0

    # --- Assets ---
    all_asset_items = []
    if not consolidated:
        bs_data.append({"Name": "<b>Assets</b>", "Value": "", "Level": 0, "IsHeader": True})
        for acc_l1 in ASSET_ACCOUNTS_L1:
             all_asset_items.extend(build_hierarchy(df, level=1, target_accounts=[acc_l1]))
        bs_data.extend(all_asset_items)
    total_assets = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(ASSET_ACCOUNTS_L1)), 'Reporting Value'].sum()
    bs_data.append({"Name": "<b>Total Assets</b>", "Value": total_assets, "Level": 0, "IsTotal": True})

    bs_data.append({"Name": "", "Value": "", "Level": 0}) # Spacer

    # --- Liabilities ---
    all_liability_items = []
    if not consolidated:
        bs_data.append({"Name": "<b>Liabilities</b>", "Value": "", "Level": 0, "IsHeader": True})
        for acc_l1 in LIABILITY_ACCOUNTS_L1:
            all_liability_items.extend(build_hierarchy(df, level=1, target_accounts=[acc_l1]))
        bs_data.extend(all_liability_items)
    total_liabilities = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(LIABILITY_ACCOUNTS_L1)), 'Reporting Value'].sum()
    bs_data.append({"Name": "<b>Total Liabilities</b>", "Value": total_liabilities, "Level": 0, "IsTotal": True})

    bs_data.append({"Name": "", "Value": "", "Level": 0}) # Spacer

    # --- Equity ---
    all_equity_items = []
    total_equity_capital = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(EQUITY_ACCOUNTS_L1)), 'Reporting Value'].sum()
    total_equity = total_equity_capital + net_profit_loss # Add current profit/loss

    if not consolidated:
        bs_data.append({"Name": "<b>Equity</b>", "Value": "", "Level": 0, "IsHeader": True})
        for acc_l1 in EQUITY_ACCOUNTS_L1:
            all_equity_items.extend(build_hierarchy(df, level=1, target_accounts=[acc_l1]))
        bs_data.extend(all_equity_items)
        # Add current period Profit/Loss under equity section
        profit_loss_label = "Current Period Profit/(Loss)"
        indent = "&nbsp;&nbsp;&nbsp;&nbsp;" # One level indent
        bs_data.append({"Name": f"{indent}{profit_loss_label}", "Value": net_profit_loss, "Level": 1}) # Treat as Level 1 item under Equity

    bs_data.append({"Name": "<b>Total Equity</b>", "Value": total_equity, "Level": 0, "IsTotal": True})

    bs_data.append({"Name": "", "Value": "", "Level": 0}) # Spacer

    # --- Total Liabilities & Equity ---
    total_liab_equity = total_liabilities + total_equity
    bs_data.append({"Name": "<b>Total Liabilities & Equity</b>", "Value": total_liab_equity, "Level": 0, "IsTotal": True})

    # --- Check Balance ---
    balance_check = total_assets - total_liab_equity
    if abs(balance_check) > 0.01: # Only show if significant difference
         bs_data.append({"Name": "", "Value": "", "Level": 0}) # Spacer
         bs_data.append({"Name": "<i>Balance Check (Assets - L&E)</i>", "Value": balance_check, "Level": 0, "IsTotal": True})


    bs_df = pd.DataFrame(bs_data)
    # Format Value column USING format_inr
    bs_df['Formatted Value'] = pd.to_numeric(bs_df['Value'], errors='coerce').apply(lambda x: format_inr(x) if pd.notnull(x) else "")
    bs_df = bs_df[bs_df['Name'] != ""] # Remove spacer rows

    # Return formatted dataframe
    return bs_df[['Name', 'Formatted Value']]


# --- Function to get Context for LLM ---
def get_llm_context(df, net_profit):
    """Prepares a concise context string for the LLM."""
    try:
        # P&L Summary
        total_income = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(INCOME_ACCOUNTS_L1)), 'Reporting Value'].sum()
        total_expense = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(EXPENSE_ACCOUNTS_L1)), 'Reporting Value'].sum()

        # BS Summary
        total_assets = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(ASSET_ACCOUNTS_L1)), 'Reporting Value'].sum()
        total_liabilities = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(LIABILITY_ACCOUNTS_L1)), 'Reporting Value'].sum()
        total_equity_capital = df.loc[(df['BaseType'] == 1) & (df['Name'].isin(EQUITY_ACCOUNTS_L1)), 'Reporting Value'].sum()
        total_equity = total_equity_capital + net_profit

        # Top 5 Expenses (Level 2)
        df_expenses_l2 = df[
            (df['BaseType'] == 2) &
            (df['GroupName'].isin(EXPENSE_ACCOUNTS_L1)) &
            (df['Reporting Value'] > 0) # Reporting value is positive for expenses
        ].nlargest(5, 'Reporting Value')
        top_expenses_str = ", ".join([f"{row['Name']} ({format_inr(row['Reporting Value'])})" for _, row in df_expenses_l2.iterrows()]) # Format INR

        context = f"""
        Financial Summary for April 2024 (Values in INR):
        - Total Income: {format_inr(total_income)}
        - Total Expenses: {format_inr(total_expense)}
        - Net Profit/(Loss): {format_inr(net_profit)}
        - Total Assets: {format_inr(total_assets)}
        - Total Liabilities: {format_inr(total_liabilities)}
        - Total Equity: {format_inr(total_equity)} (Includes Capital & Current Profit/Loss)
        - Top 5 Level 2 Expense Accounts: {top_expenses_str}
        - Data Columns available: TallyExportDate, MISMonthDate, BaseType, Name, GroupName, DebitAmt, CreditAmt, Net Effect, Reporting Value, Abs Net Effect
        """
        return context.strip()
    except Exception as e:
        st.warning(f"Could not generate full context: {e}")
        return "Basic financial data summary could not be fully generated."

# --- Function to query OpenAI ---
def ask_openai_for_chart(api_key, context, question):
    """
    Sends question and context to OpenAI API, requesting JSON output
    that includes a text answer and potentially chart parameters.
    """
    if not api_key:
        return json.dumps({"error": "OpenAI API key is missing."})
    try:
        openai.api_key = api_key
        prompt = f"""You are an expert financial analyst AI assistant. Analyze the following financial summary context for April 2024 (values in INR) and the user's question.

Context:
{context}

User Question: {question}

Your Task:
1. Provide a concise text answer to the user's question based ONLY on the provided context.
2. If the user explicitly asks for a chart (e.g., "show me a pie chart", "create a bar graph", "plot expenses"), identify the parameters needed to create that chart using the available data. If a chart is requested, include a 'chart_info' object in your response.
3. Structure your entire response as a JSON object with two keys: 'text_answer' (string) and 'chart_info' (object or null).

Details for 'chart_info' (only include if a chart is requested):
- "type": Specify the chart type requested (e.g., "pie", "bar"). Only support "pie" and "bar" for now.
- "title": Suggest a suitable title for the chart.
- "data_filter": Describe the filtering needed on the raw data (e.g., {{"BaseType": 2, "GroupName": ["Direct Expenses", "Indirect Expenses"]}} to get Level 2 expenses). Use column names from the context. Filter based on the user's request (e.g., filter by BaseType, GroupName, or Name). Use a dictionary where keys are column names and values are the filter criteria (can be a single value or a list of values).
- "labels_column": The column name to be used for chart labels (usually 'Name').
- "values_column": The column name for chart values (usually 'Reporting Value' or 'Abs Net Effect').

Example JSON response if a chart IS requested:
{{
  "text_answer": "Here is a breakdown of Level 2 expenses...",
  "chart_info": {{
    "type": "pie",
    "title": "Level 2 Expense Composition",
    "data_filter": {{ "BaseType": 2, "GroupName": ["Direct Expenses", "Indirect Expenses"] }},
    "labels_column": "Name",
    "values_column": "Reporting Value"
  }}
}}

Example JSON response if NO chart is requested:
{{
  "text_answer": "The net profit for April 2024 is ₹ XX,XX,XXX.XX.",
  "chart_info": null
}}

Provide only the JSON object in your response. Ensure the JSON is valid.

JSON Response:
"""

        response = openai.chat.completions.create(
            model="gpt-4o", # Recommend gpt-4o or gpt-4-turbo for better JSON/reasoning
            messages=[
                {"role": "system", "content": "You are an AI assistant that provides financial analysis and responds in valid JSON format."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=500, # Allow more tokens for complex responses
            temperature=0.2,
            response_format={"type": "json_object"} # Enforce JSON output
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        st.error(f"Error during OpenAI API call: {e}") # Show error in UI
        return json.dumps({"error": f"Error interacting with OpenAI: {e}"})


# --- Function to Generate Chart based on AI parameters ---
def generate_chart_from_ai(df, chart_info):
    """Takes chart parameters from LLM and generates a Plotly chart."""
    if not chart_info or not isinstance(chart_info, dict):
        return None, "Invalid chart information received from AI."

    try:
        chart_type = chart_info.get("type", "").lower()
        title = chart_info.get("title", "AI Generated Chart")
        data_filter = chart_info.get("data_filter")
        labels_col = chart_info.get("labels_column")
        values_col = chart_info.get("values_column")

        # Basic validation of parameters
        if not all([chart_type, title, data_filter, labels_col, values_col]):
            return None, "Missing required chart parameters from AI (type, title, data_filter, labels_column, values_column)."
        if chart_type not in ["pie", "bar"]:
             return None, f"Unsupported chart type requested by AI: '{chart_type}'. Only 'pie' and 'bar' are supported."
        if values_col not in df.columns or labels_col not in df.columns:
             return None, f"Invalid columns specified by AI: '{labels_col}' or '{values_col}' not found in data."

        # Filter the dataframe
        df_chart = df.copy()
        if isinstance(data_filter, dict):
            for col, criteria in data_filter.items():
                if col in df_chart.columns:
                    # Ensure criteria is a list for isin()
                    if not isinstance(criteria, list):
                        criteria = [criteria]

                    # Attempt filtering, handle potential type mismatches
                    try:
                        # Get the dtype of the column
                        col_dtype = df_chart[col].dtype
                        # Convert criteria elements to column type if possible/necessary
                        # This part is tricky and might need refinement based on typical criteria formats from LLM
                        if pd.api.types.is_numeric_dtype(col_dtype):
                            processed_criteria = [pd.to_numeric(c) for c in criteria if c is not None]
                        elif pd.api.types.is_string_dtype(col_dtype):
                             processed_criteria = [str(c) for c in criteria if c is not None]
                        else: # For other types (like datetime), assume criteria is already compatible or handle specifically
                            processed_criteria = criteria

                        df_chart = df_chart[df_chart[col].isin(processed_criteria)]

                    except Exception as filter_err:
                        st.warning(f"Could not apply filter on column '{col}' with criteria '{criteria}': {filter_err}. Skipping this filter.")

                else:
                    # Ignore filter if column doesn't exist, but warn
                    st.warning(f"Filter column '{col}' specified by AI not found in data.")
        else:
            return None, "Invalid data_filter format from AI (should be a dictionary)."

        # Ensure values are numeric and suitable for plotting
        df_chart[values_col] = pd.to_numeric(df_chart[values_col], errors='coerce')
        df_chart = df_chart.dropna(subset=[values_col])
        # Use absolute value for plot height/size, handle potential zeros
        plot_values_col = 'Abs Plot Value'
        df_chart[plot_values_col] = df_chart[values_col].abs()
        df_chart = df_chart[df_chart[plot_values_col] > 0.01] # Filter out tiny/zero values

        if df_chart.empty:
            return None, "No data found matching the criteria specified by AI after filtering and cleaning."

        # Generate chart based on type
        fig = None
        # Prepare hover data with INR format using the original value column sign
        df_chart['Value_INR'] = df_chart[values_col].apply(format_inr)

        plot_template = 'plotly_white' # Use a default template

        if chart_type == "pie":
            fig = px.pie(df_chart, names=labels_col, values=plot_values_col, title=title, hole=0.3)
            fig.update_traces(textposition='inside', textinfo='percent+label',
                              hovertemplate='<b>%{label}</b><br>Amount: %{customdata[0]}<extra></extra>',
                              customdata=df_chart[['Value_INR']])
        elif chart_type == "bar":
            df_chart = df_chart.sort_values(by=plot_values_col, ascending=False)
            fig = px.bar(df_chart, x=labels_col, y=plot_values_col, title=title,
                         labels={plot_values_col: 'Amount (Absolute Value)', labels_col: ''},
                         text=plot_values_col)
            fig.update_traces(texttemplate='%{text:,.0f}', textposition='outside',
                              hovertemplate='<b>%{x}</b><br>Amount: %{customdata[0]}<extra></extra>',
                              customdata=df_chart[['Value_INR']])
            fig.update_layout(yaxis_title="Amount (INR, Absolute)")

        # Apply general layout settings
        if fig:
            fig.update_layout(title_x=0.5, legend_title_text='Categories', template=plot_template)
            return fig, None # Return figure and no error
        else:
             return None, "Failed to generate chart figure." # Should be caught by earlier checks

    except Exception as e:
        import traceback
        st.error(f"Error during chart generation: {e}\n{traceback.format_exc()}") # Log full traceback for debugging
        return None, f"An unexpected error occurred while generating the chart: {e}"

# --- Streamlit App UI ---
st.title("📊 Financial Insights Dashboard (INR)")

# Add session state initialization for dynamic elements if needed
if 'current_time' not in st.session_state:
     from datetime import datetime
     import pytz
     try:
        # Example: Use a timezone relevant to the user if known, otherwise UTC or local
        # tz = pytz.timezone("Asia/Dubai") # User mentioned Dubai previously
        tz = pytz.timezone("Asia/Kolkata") # Example: India timezone
        st.session_state['current_time'] = datetime.now(tz).strftime("%I:%M:%S %p %Z")
        st.session_state['location'] = tz.zone
     except Exception: # Fallback
          st.session_state['current_time'] = datetime.now().strftime("%I:%M:%S %p")
          st.session_state['location'] = "your location"

st.caption(f"Displaying data for the period ending April 2024. Current time in {st.session_state.get('location', 'your location')} is {st.session_state.get('current_time', 'unknown')}.")

# --- File Uploader ---
uploaded_file = st.file_uploader("Choose your Tally Export Excel file (.xlsx)", type="xlsx", help="Upload the Excel file containing Levels 1-5 financial data in one sheet.")

if uploaded_file is not None:
    # Wrap data loading and processing in a spinner
    with st.spinner(f"Loading and processing '{uploaded_file.name}'..."):
        df_raw = load_data(uploaded_file)

    if df_raw is not None:
        st.success(f"✅ File '{uploaded_file.name}' processed successfully!")

        # --- Calculate Key Figures Once ---
        try:
            pnl_df_calc, net_profit_for_bs = generate_pnl(df_raw, consolidated=True) # Use raw numerical profit/loss
            total_income = df_raw.loc[(df_raw['BaseType'] == 1) & (df_raw['Name'].isin(INCOME_ACCOUNTS_L1)), 'Reporting Value'].sum()
            total_expense = df_raw.loc[(df_raw['BaseType'] == 1) & (df_raw['Name'].isin(EXPENSE_ACCOUNTS_L1)), 'Reporting Value'].sum()
            profit_margin = (net_profit_for_bs / total_income * 100) if total_income else 0
            total_equity_capital = df_raw.loc[(df_raw['BaseType'] == 1) & (df_raw['Name'].isin(EQUITY_ACCOUNTS_L1)), 'Reporting Value'].sum()
            total_equity = total_equity_capital + net_profit_for_bs
        except Exception as calc_error:
            st.error(f"Error during initial calculations: {calc_error}. Some KPIs or reports might be unavailable.")
            # Set defaults to avoid crashing later sections
            net_profit_for_bs, total_income, total_expense, profit_margin, total_equity = 0, 0, 0, 0, 0


        # --- Sidebar ---
        st.sidebar.header("👤 User Role & Filters")
        selected_role = st.sidebar.selectbox(
            "Select Your Role",
            ("Accountant", "CFO", "CEO"),
            help="Select your role to customize the dashboard view."
        )
        st.sidebar.divider()
        st.sidebar.subheader("⚙️ Data Filters")
        # --- Level Filter ---
        min_level = int(df_raw['BaseType'].min())
        max_level = int(df_raw['BaseType'].max())
        selected_level = st.sidebar.slider(
            "Select Hierarchy Level (BaseType)",
            min_value=min_level,
            max_value=max_level,
            value=(min_level, max_level),
            help="Filter data based on the hierarchy level (1=Top Level)."
        )
        # Apply level filter first
        df_filtered_sidebar = df_raw[(df_raw['BaseType'] >= selected_level[0]) & (df_raw['BaseType'] <= selected_level[1])]

        # --- Group Filter ---
        if 'GroupName' in df_filtered_sidebar.columns and not df_filtered_sidebar.empty:
            # Get relevant group names for selection based on the filtered levels
            parent_groups = df_filtered_sidebar[df_filtered_sidebar['BaseType'] > 1]['GroupName'].unique()
            level1_names_in_view = df_filtered_sidebar[df_filtered_sidebar['BaseType'] == 1]['Name'].unique()
            # Offer filtering primarily by Level 1 concepts for simplicity, unless L1 is filtered out
            filter_options = sorted(list(set(ASSET_ACCOUNTS_L1 + LIABILITY_ACCOUNTS_L1 + EQUITY_ACCOUNTS_L1 + INCOME_ACCOUNTS_L1 + EXPENSE_ACCOUNTS_L1)))

            selected_groups = st.sidebar.multiselect(
                "Filter by Level 1 Group/Account",
                options=filter_options,
                default=[],
                help="Select top-level groups to focus the analysis across all views."
            )
            # Apply group filter: Show items if they are selected L1 OR if their parent group is selected L1
            if selected_groups:
                 # Need a way to map any item back to its L1 parent for filtering
                 # This simple filter only works if GroupName directly maps to L1, may need refinement
                 # For now, filter based on direct L1 name or immediate parent being L1
                 df_filtered_sidebar = df_filtered_sidebar[
                     df_filtered_sidebar['Name'].isin(selected_groups) & (df_filtered_sidebar['BaseType'] == 1) |
                     df_filtered_sidebar['GroupName'].isin(selected_groups)
                 ]

        st.sidebar.divider()
        st.sidebar.caption(f"Dashboard loaded at {st.session_state.get('current_time', '')}")
        st.sidebar.caption(f"Location: {st.session_state.get('location', '')}")

        # --- Main Content Tabs ---
        tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
            "📊 KPI Dashboard",
            "📈 Profit & Loss",
            "🧾 Balance Sheet",
            "🎨 Visual Insights",
            "🔍 Data Explorer",
            "🤖 AI Assistant"
        ])

        # --- Tab 1: Role-Based Dashboard ---
        with tab1:
            # (Keep improved layout from previous step)
             st.header(f"📊 {selected_role} Dashboard")
             st.markdown(f"Key financial snapshot for **April 2024** (Values in INR). Tailored for **{selected_role}**.")
             st.divider()

             st.subheader("Key Performance Indicators")
             col1, col2, col3 = st.columns(3)
             with col1:
                 st.markdown("##### 💰 Income")
                 st.metric("Total Income", format_inr(total_income))
             with col2:
                 st.markdown("##### 💸 Expenses")
                 st.metric("Total Expenses", format_inr(total_expense))
             with col3:
                 st.markdown("##### ✅ Profitability")
                 st.metric("Net Profit/(Loss)", format_inr(net_profit_for_bs))
                 if selected_role in ["CFO", "CEO"]:
                     st.metric("Profit Margin", f"{profit_margin:.1f}%", help="Net Profit / Total Income")

             st.divider()
             if selected_role == "CFO":
                 st.subheader("Financial Ratios")
                 rcol1, rcol2 = st.columns(2)
                 with rcol1:
                     try:
                         current_assets = df_raw.loc[(df_raw['BaseType'] == 1) & (df_raw['Name'] == 'Current Assets'), 'Reporting Value'].iloc[0]
                         current_liab = df_raw.loc[(df_raw['BaseType'] == 1) & (df_raw['Name'] == 'Current Liabilities'), 'Reporting Value'].iloc[0]
                         current_ratio = current_assets / current_liab if current_liab else 0
                         st.metric("Current Ratio", f"{current_ratio:.2f}", help="Current Assets / Current Liabilities")
                         st.caption("Measures short-term liquidity.")
                     except Exception as e:
                         st.metric("Current Ratio", "N/A")
                         st.caption(f"Could not calculate.")
                 with rcol2:
                      try:
                         total_liab = df_raw.loc[(df_raw['BaseType'] == 1) & (df_raw['Name'].isin(LIABILITY_ACCOUNTS_L1)), 'Reporting Value'].sum()
                         debt_equity_ratio = total_liab / total_equity if total_equity else 0
                         st.metric("Debt-to-Equity", f"{debt_equity_ratio:.2f}", help="Total Liabilities / Total Equity")
                         st.caption("Measures financial leverage.")
                      except Exception as e:
                           st.metric("Debt-to-Equity", "N/A")
                           st.caption(f"Could not calculate.")

             if selected_role == "CEO":
                  st.subheader("Quick Visuals")
                  try:
                      df_summary = pd.DataFrame({
                           'Category': ['Total Income', 'Total Expenses'],
                           'Amount': [total_income, total_expense]
                      })
                      fig_ceo = px.bar(df_summary, x='Category', y='Amount', text='Amount', title="Income vs Expenses Overview")
                      fig_ceo.update_traces(texttemplate='%{text:,.0f}', textposition='auto')
                      fig_ceo.update_layout(template='plotly_white', height=300, yaxis_title="Amount (INR)")
                      st.plotly_chart(fig_ceo, use_container_width=True)
                  except Exception as e:
                       st.warning(f"Could not display CEO visual: {e}")


        # --- Tab 2: Profit & Loss ---
        with tab2:
            st.header("📈 Profit & Loss Statement (INR)")
            st.caption("Displays income and expenses for the period.")
            default_pnl_view = "Detailed" if selected_role == "Accountant" else "Consolidated"
            index_pnl = 0 if default_pnl_view == "Detailed" else 1
            view_pnl = st.radio("Select P&L View", ("Detailed", "Consolidated"), index=index_pnl, horizontal=True, key="pnl_view")
            consolidated_pnl = (view_pnl == "Consolidated")
            try:
                 # Use a copy of df_raw to avoid modifying original data
                 pnl_statement_df, _ = generate_pnl(df_raw.copy(), consolidated=consolidated_pnl)
                 # Use st.markdown to render HTML for indentation
                 st.markdown(pnl_statement_df.to_html(escape=False, index=False, header=False), unsafe_allow_html=True)
            except Exception as e:
                 st.error(f"Could not generate P&L Statement: {e}")

        # --- Tab 3: Balance Sheet ---
        with tab3:
            st.header("🧾 Balance Sheet (INR)")
            st.caption("Snapshot of Assets, Liabilities, and Equity at the end of the period.")
            default_bs_view = "Detailed" if selected_role == "Accountant" else "Consolidated"
            index_bs = 0 if default_bs_view == "Detailed" else 1
            view_bs = st.radio("Select Balance Sheet View", ("Detailed", "Consolidated"), index=index_bs, horizontal=True, key="bs_view")
            consolidated_bs = (view_bs == "Consolidated")
            try:
                # Use a copy of df_raw
                 bs_statement_df = generate_bs(df_raw.copy(), net_profit_for_bs, consolidated=consolidated_bs)
                 st.markdown(bs_statement_df.to_html(escape=False, index=False, header=False), unsafe_allow_html=True)
            except Exception as e:
                 st.error(f"Could not generate Balance Sheet: {e}")


        # --- Tab 4: Visual Insights ---
        with tab4:
            # (Keep chart generation logic same as before, including captions and role-based visibility)
            st.header("🎨 Visual Insights (INR)")
            st.caption("Interactive charts exploring different financial categories.")

            # Expense Chart
            if selected_role in ["CFO", "Accountant"]:
                st.subheader("Expense Composition (Level 2 Accounts)")
                try:
                    df_expenses_l2 = df_raw[
                        (df_raw['BaseType'] == 2) &
                        (df_raw['GroupName'].isin(EXPENSE_ACCOUNTS_L1)) &
                        (df_raw['Reporting Value'] > 0) # Use positive reporting value
                    ].copy()
                    if not df_expenses_l2.empty:
                        df_expenses_l2['Value_INR'] = df_expenses_l2['Reporting Value'].apply(format_inr)
                        fig_exp = px.pie(df_expenses_l2, names='Name', values='Reporting Value', title="Level 2 Expenses by Account", hole=0.3)
                        fig_exp.update_traces(textposition='inside', textinfo='percent+label', hovertemplate='<b>%{label}</b><br>Amount: %{customdata[0]}<extra></extra>', customdata=df_expenses_l2[['Value_INR']])
                        fig_exp.update_layout(template='plotly_white')
                        st.plotly_chart(fig_exp, use_container_width=True)
                        st.caption("Pie chart showing the proportion of different Level 2 expense accounts.")
                    else:
                        st.info("No Level 2 expense data found to display.")
                except Exception as e:
                    st.warning(f"Could not generate expense chart: {e}")
            else:
                 st.info("Detailed expense charts available for CFO/Accountant roles.")

            # Income Chart
            st.subheader("Income Sources (Level 1 Accounts)")
            try:
                df_income_l1 = df_raw[
                     (df_raw['BaseType'] == 1) &
                     (df_raw['Name'].isin(INCOME_ACCOUNTS_L1)) &
                     (df_raw['Reporting Value'] > 0) # Use positive reporting value
                ].copy()
                if not df_income_l1.empty:
                     df_income_l1['Value_INR'] = df_income_l1['Reporting Value'].apply(format_inr)
                     fig_inc = px.bar(df_income_l1, x='Name', y='Reporting Value', title="Level 1 Income by Account", labels={'Reporting Value': 'Amount (INR)', 'Name': 'Income Account'}, text='Reporting Value')
                     fig_inc.update_traces(texttemplate='%{text:,.0f}', textposition='outside', hovertemplate='<b>%{x}</b><br>Amount: %{customdata[0]}<extra></extra>', customdata=df_income_l1[['Value_INR']])
                     fig_inc.update_layout(xaxis_title=None, yaxis_title="Amount (INR)", template='plotly_white')
                     st.plotly_chart(fig_inc, use_container_width=True)
                     st.caption("Bar chart comparing different Level 1 income sources.")
                else:
                     st.info("No Level 1 income data found to display.")
            except Exception as e:
                 st.warning(f"Could not generate income chart: {e}")

            # Asset Chart
            st.subheader("Asset Composition (Level 1 Accounts)")
            try:
                df_assets_l1 = df_raw[
                    (df_raw['BaseType'] == 1) &
                    (df_raw['Name'].isin(ASSET_ACCOUNTS_L1)) &
                    (df_raw['Reporting Value'].abs() > 0.01) # Use non-zero absolute reporting value
                ].copy()
                if not df_assets_l1.empty:
                      df_assets_l1['Value_INR'] = df_assets_l1['Reporting Value'].apply(format_inr)
                      fig_assets = px.pie(df_assets_l1, names='Name', values='Reporting Value', title="Asset Composition", hole=0.3)
                      fig_assets.update_traces(textposition='inside', textinfo='percent+label', hovertemplate='<b>%{label}</b><br>Amount: %{customdata[0]}<extra></extra>', customdata=df_assets_l1[['Value_INR']])
                      fig_assets.update_layout(template='plotly_white')
                      st.plotly_chart(fig_assets, use_container_width=True)
                      st.caption("Pie chart showing the proportion of different Level 1 asset categories.")
                else:
                      st.info("No Level 1 asset data found to display.")
            except Exception as e:
                 st.warning(f"Could not generate asset chart: {e}")

            # Liability Chart
            st.subheader("Liability Composition (Level 1 Accounts)")
            try:
                df_liab_l1 = df_raw[
                     (df_raw['BaseType'] == 1) &
                     (df_raw['Name'].isin(LIABILITY_ACCOUNTS_L1)) &
                     (df_raw['Reporting Value'] > 0) # Use positive reporting value
                 ].copy()
                if not df_liab_l1.empty:
                      df_liab_l1['Value_INR'] = df_liab_l1['Reporting Value'].apply(format_inr)
                      fig_liab = px.pie(df_liab_l1, names='Name', values='Reporting Value', title="Liability Composition", hole=0.3)
                      fig_liab.update_traces(textposition='inside', textinfo='percent+label', hovertemplate='<b>%{label}</b><br>Amount: %{customdata[0]}<extra></extra>', customdata=df_liab_l1[['Value_INR']])
                      fig_liab.update_layout(template='plotly_white')
                      st.plotly_chart(fig_liab, use_container_width=True)
                      st.caption("Pie chart showing the proportion of different Level 1 liability categories.")
                else:
                      st.info("No Level 1 liability data found to display.")
            except Exception as e:
                 st.warning(f"Could not generate liability chart: {e}")


        # --- Tab 5: Raw Data Explorer ---
        with tab5:
             st.header("🔍 Data Explorer")
             if selected_role == "Accountant":
                 st.caption("Detailed view of the raw data. Use sidebar filters to narrow down.")
                 st.markdown(f"Showing **{len(df_filtered_sidebar)} rows** based on current filters.")
                 if selected_groups:
                     st.markdown(f"Filtered by Level 1 Groups: **{', '.join(selected_groups)}**")
                 # Display dataframe; consider using AgGrid for better interaction if needed later
                 st.dataframe(df_filtered_sidebar, hide_index=True, use_container_width=True)
             else:
                 st.info("Detailed Raw Data Explorer is available for the Accountant role.")
                 st.caption("Select the 'Accountant' role in the sidebar for full data access.")


        # --- Tab 6: AI Assistant ---
        with tab6:
            st.header("🤖 AI Financial Assistant")
            st.warning("Note: AI responses are based on summary data and may fail or be inaccurate. Verify critical information. Chart generation is experimental.", icon="⚠️")

            if not OPENAI_API_KEY:
                st.error("OpenAI API Key not found in `.env` file. Please set it up to use the AI Assistant.")
            else:
                # Provide example questions
                st.markdown("Example questions:")
                st.markdown("- *What is the total current assets?*")
                st.markdown("- *Show a bar chart of Level 1 income sources.*")
                st.markdown("- *List the top 5 expenses.*")
                st.markdown("- *Create a pie chart for Level 2 indirect expenses.*")

                user_question = st.text_area("Ask a question about the April 2024 financial summary:", key="ai_question", placeholder="Type your question here...")

                if st.button("Ask AI", key="ai_ask_button", type="primary"): # Use primary button style
                    if not user_question:
                        st.error("Please enter a question.")
                    else:
                        with st.spinner("AI Assistant is thinking..."):
                            try:
                                # 1. Generate Context
                                context_str = get_llm_context(df_raw, net_profit_for_bs)
                                # 2. Ask OpenAI
                                ai_response_json_str = ask_openai_for_chart(OPENAI_API_KEY, context_str, user_question)
                                # 3. Parse and Display
                                ai_response_data = json.loads(ai_response_json_str)

                                st.markdown("---")
                                st.markdown("**AI Response:**")
                                if "text_answer" in ai_response_data:
                                    st.markdown(ai_response_data["text_answer"])
                                elif "error" in ai_response_data:
                                     st.error(f"AI Error: {ai_response_data['error']}")
                                else:
                                    st.warning("AI did not provide a text answer in the expected format.")

                                # 4. Attempt Chart Generation
                                if "chart_info" in ai_response_data and ai_response_data["chart_info"]:
                                    st.markdown("**AI Generated Chart:**")
                                    with st.spinner("Generating chart based on AI parameters..."):
                                        fig_ai, error_msg_ai = generate_chart_from_ai(df_raw, ai_response_data["chart_info"])
                                    if fig_ai:
                                        st.plotly_chart(fig_ai, use_container_width=True)
                                    else:
                                        st.warning(f"Could not generate chart: {error_msg_ai}")

                            except json.JSONDecodeError:
                                st.error("AI response could not be parsed (invalid JSON). Raw response:")
                                st.text(ai_response_json_str if 'ai_response_json_str' in locals() else "No response received")
                            except Exception as e:
                                 st.error(f"An error occurred processing the AI response: {e}")
                                 import traceback
                                 st.error(f"Traceback: {traceback.format_exc()}")


    else:
        # Only show error if file was uploaded but failed processing
        if uploaded_file:
             st.error("Could not process the uploaded file. Please check the file format and content.")

else:
    # Initial landing message
    st.info("👈 Please upload an Excel file using the uploader above to get started!")
    st.markdown("Ensure your OpenAI API key is set in the `.env` file to use the AI Assistant tab.")


# --- End of Script ---