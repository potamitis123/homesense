# Product Guidelines

## 1. Tone and Voice

The application's communication style should be:
- **Technical and Analytical:** Emphasize precision and data-driven insights, suitable for a researcher/analyst audience.
- **Clear and Concise:** Ensure information is easily understood and actionable.
- **Empathetic and Supportive:** Focus on user needs and provide helpful guidance.

## 2. Design and User Experience

The user interface and experience should adhere to the following principles:
- **Clean and Minimalist Interface:** Prioritize a clutter-free design that focuses on clarity and ease of use, especially when dealing with complex data.
- **Intuitive Data Visualization:** Present audio data and analysis results in a way that is immediately understandable and insightful.
- **Responsive Design:** Guarantee a seamless and consistent experience across different devices and screen sizes.

## 3. Data Retention and Backup Policy

- **Data Retention:** The SQL database will retain a rolling window of 14 days of data.
- **Backup:** Each day, the oldest day's data from the rolling window will be compressed into a zip file and archived for backup purposes.
