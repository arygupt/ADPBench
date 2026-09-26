module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam integer A_BEATS   = ROWS * COLS / LANES; // 64
    localparam integer X_BEATS   = COLS / LANES;        // 4
    localparam integer ROW_BEATS = COLS / LANES;        // 4
    localparam integer X_W       = LANES * DATA_W;      // 128

    // Four x beats are stored as they arrive.  A is not buffered: each A beat
    // is consumed immediately and contributes 16 products for the current row.
    logic [X_W-1:0]              x_mem [0:X_BEATS-1];
    logic [2:0]                  x_cnt;   // x beats stored in current txn (0..4)
    logic [6:0]                  a_idx;   // next A beat to consume (0..64)
    logic [1:0]                  a_seg;   // which quarter of the current row
    logic signed [ACC_W-1:0]     acc;     // current row accumulator
    logic                        out_valid_r;  // registered, backpressured output
    logic signed [ACC_W-1:0]     out_c_r;

    // Byte lanes for one A beat and the matching x quarter.
    logic signed [DATA_W-1:0]    a_byte [0:LANES-1];
    logic signed [DATA_W-1:0]    x_byte [0:LANES-1];
    logic signed [ACC_W-1:0]     prod_sum [0:LANES];
    logic signed [ACC_W-1:0]     dot;

    assign a_seg = a_idx[1:0];

    wire x_hs   = in_x_flat_valid && in_x_flat_ready;
    wire a_hs   = in_a_flat_valid && in_a_flat_ready;
    wire out_comb = a_hs && (a_seg == ROW_BEATS - 1);
    wire out_valid = out_valid_r || out_comb;
    wire out_hs = out_valid && out_ready;

    // Final output word of a transaction can be accepted in the same cycle as
    // the final A beat (out_comb), or later from the registered output.
    wire txn_done_comb = a_hs && (a_seg == ROW_BEATS - 1) &&
                         (a_idx == A_BEATS - 1) && out_ready;
    wire txn_done_pend = out_hs && out_valid_r && (a_idx == A_BEATS);

    // Accept x while this transaction still needs x beats.
    assign in_x_flat_ready = (x_cnt < X_BEATS);

    // Consume an A beat only when its matching x quarter is already stored
    // and no registered output is still waiting.
    assign in_a_flat_ready = (!out_valid_r) &&
                             (a_idx < A_BEATS) &&
                             (x_cnt > a_seg);

    assign out_c = out_valid_r ? out_c_r : (acc + dot);

    // Split the current A beat and the selected x quarter into signed bytes.
    always_comb begin
        integer i;
        for (i = 0; i < LANES; i = i + 1) begin
            a_byte[i] = $signed(in_a_flat[i*DATA_W +: DATA_W]);
            x_byte[i] = $signed(x_mem[a_seg][i*DATA_W +: DATA_W]);
        end
    end

    // Dot product of one 16-element quarter.
    always_comb begin
        integer i;
        prod_sum[0] = 32'sd0;
        for (i = 0; i < LANES; i = i + 1) begin
            prod_sum[i+1] = prod_sum[i] + a_byte[i] * x_byte[i];
        end
        dot = prod_sum[LANES];
    end

    // A-side state: accumulator, A beat counter, registered output.
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_idx       <= '0;
            acc         <= '0;
            out_valid_r <= 1'b0;
            out_c_r     <= '0;
        end else begin
            if (txn_done_comb) begin
                // Last output accepted combinationally with the final A beat.
                a_idx       <= '0;
                acc         <= '0;
                out_valid_r <= 1'b0;
                out_c_r     <= '0;
            end else if (txn_done_pend) begin
                // Last registered output accepted: transaction complete.
                a_idx       <= '0;
                acc         <= '0;
                out_valid_r <= 1'b0;
                out_c_r     <= '0;
            end else if (a_hs) begin
                a_idx <= a_idx + 1'b1;
                if (a_seg == ROW_BEATS - 1) begin
                    if (out_ready) begin
                        // Result consumed combinationally this cycle.
                        out_valid_r <= 1'b0;
                        out_c_r     <= '0;
                    end else begin
                        // Register the result until the output side accepts it.
                        out_valid_r <= 1'b1;
                        out_c_r     <= acc + dot;
                    end
                    acc <= '0;
                end else begin
                    acc         <= acc + dot;
                    out_valid_r <= 1'b0;
                end
            end else if (out_hs) begin
                out_valid_r <= 1'b0;
            end
        end
    end

    // x-side state.  x beats are written in order and the counter is cleared
    // when the transaction's last output word has been accepted.
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_cnt <= '0;
        end else begin
            if (x_hs) begin
                x_mem[x_cnt] <= in_x_flat;
                x_cnt        <= x_cnt + 1'b1;
            end else if (txn_done_comb || txn_done_pend) begin
                x_cnt <= '0;
            end
        end
    end

endmodule
