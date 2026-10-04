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

    localparam COL_GROUPS   = COLS / LANES;       // 4
    localparam LAST_COL_GRP = COL_GROUPS - 1;     // 3
    localparam X_BEATS      = COL_GROUPS;          // 4
    localparam LAST_X_BEAT  = X_BEATS - 1;        // 3
    localparam LAST_ROW     = ROWS - 1;            // 15

    // FSM states
    localparam S_RECV_X  = 2'd0;
    localparam S_RECV_A  = 2'd1;
    localparam S_OUT_ROW = 2'd2;

    reg [1:0] state;
    reg [5:0] x_cnt;
    reg [5:0] a_cnt;
    reg [3:0] row_idx;
    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] out_reg;

    // x buffer: [col_group 0..3][lane 0..15]
    reg signed [DATA_W-1:0] x_buf [0:COL_GROUPS-1][0:LANES-1];

    // Extract signed lane values from flat input buses
    wire signed [DATA_W-1:0] a_lane [0:LANES-1];
    wire signed [DATA_W-1:0] x_lane [0:LANES-1];

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : gen_extract
            assign a_lane[gi] = in_a_flat[gi*DATA_W +: DATA_W];
            assign x_lane[gi] = in_x_flat[gi*DATA_W +: DATA_W];
        end
    endgenerate

    // Current column group from A beat counter
    wire [1:0] col_grp = a_cnt[1:0];

    // Combinational: 16-element partial dot product
    // sum_{j=0}^{15} a_lane[j] * x_buf[col_grp][j]
    reg signed [ACC_W-1:0] partial_sum;
    integer j;
    always @(*) begin
        partial_sum = {ACC_W{1'b0}};
        for (j = 0; j < LANES; j = j + 1) begin
            partial_sum = partial_sum + $signed(a_lane[j]) * $signed(x_buf[col_grp][j]);
        end
    end

    // Sequential FSM
    integer k;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state   <= S_RECV_X;
            x_cnt   <= 6'd0;
            a_cnt   <= 6'd0;
            row_idx <= 4'd0;
            acc     <= {ACC_W{1'b0}};
            out_reg <= {ACC_W{1'b0}};
        end else begin
            case (state)
                // Phase 1: receive x vector (4 beats of 16 elements)
                S_RECV_X: begin
                    if (in_x_flat_valid && in_x_flat_ready) begin
                        for (k = 0; k < LANES; k = k + 1)
                            x_buf[x_cnt[1:0]][k] <= x_lane[k];
                        if (x_cnt == LAST_X_BEAT[5:0]) begin
                            state   <= S_RECV_A;
                            a_cnt   <= 6'd0;
                            row_idx <= 4'd0;
                            acc     <= {ACC_W{1'b0}};
                        end
                        x_cnt <= x_cnt + 6'd1;
                    end
                end

                // Phase 2: receive A beats, compute on-the-fly
                S_RECV_A: begin
                    if (in_a_flat_valid && in_a_flat_ready) begin
                        if (col_grp == LAST_COL_GRP[1:0]) begin
                            // Last column group for this row -> store result
                            out_reg <= acc + partial_sum;
                            acc     <= {ACC_W{1'b0}};
                            state   <= S_OUT_ROW;
                        end else begin
                            acc <= acc + partial_sum;
                        end
                        a_cnt <= a_cnt + 6'd1;
                    end
                end

                // Phase 3: output one row result
                S_OUT_ROW: begin
                    if (out_valid && out_ready) begin
                        if (row_idx == LAST_ROW[3:0]) begin
                            // All rows done; prepare for next transaction
                            state   <= S_RECV_X;
                            x_cnt   <= 6'd0;
                            a_cnt   <= 6'd0;
                            row_idx <= 4'd0;
                            acc     <= {ACC_W{1'b0}};
                        end else begin
                            row_idx <= row_idx + 4'd1;
                            state   <= S_RECV_A;
                        end
                    end
                end

                default: state <= S_RECV_X;
            endcase
        end
    end

    // Handshake and output assignments
    assign in_x_flat_ready = (state == S_RECV_X);
    assign in_a_flat_ready = (state == S_RECV_A);
    assign out_valid       = (state == S_OUT_ROW);
    assign out_c           = out_reg;

endmodule
